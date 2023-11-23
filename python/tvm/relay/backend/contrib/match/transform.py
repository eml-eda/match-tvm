# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""Relay transformations for MATCH"""

import numpy as np
import tvm
from tvm import relay
from tvm.relay import transform
from tvm.relay.expr_functor import ExprMutator, ExprVisitor
from tvm.relay.dataflow_pattern import DFPatternCallback, rewrite, wildcard, is_op, is_constant


class MatchOnnxDigitalRequantRewriter(DFPatternCallback):
    """Rewriter for digital requant pattern
    """
    def __init__(self, require_type=False):
        super().__init__(require_type)

        self.x = wildcard()
        self.div1 = is_constant()
        self.div2 = is_constant()
        self.maximum = is_constant()
        self.minimum = is_constant()

        cast = is_op("cast")(self.x)
        div1 = is_op("divide")(cast, self.div1)
        div2 = is_op("divide")(div1, self.div2)
        floor = is_op("floor")(div2)
        maximum = is_op("maximum")(floor, self.maximum)
        minimum = is_op("minimum")(maximum, self.minimum)
        self.pattern = is_op("cast")(minimum)

    def callback(self, pre, post, node_map):
        x = node_map[self.x][0]
        div1 = node_map[self.div1][0]
        div2 = node_map[self.div2][0]
        maximum = node_map[self.maximum][0]
        minimum = node_map[self.minimum][0]

        shift_factor = int(np.log2(div1.data.numpy() * div2.data.numpy()))

        x = relay.op.right_shift(x, relay.const(shift_factor))
        x = relay.op.clip(x, a_min=int(maximum.data.numpy()), a_max=int(minimum.data.numpy()))
        return relay.op.cast(x, 'uint8')


@tvm.ir.transform.module_pass(opt_level=0)
class MatchOnnxRequantTransform:
    """ Find and rewrite MATCH ONNX requant to requant for internal use:
        div->div->floor->max->min to
        right_shift->clip->cast
    """
    def transform_module(
        self, mod: tvm.ir.IRModule, ctx: tvm.ir.transform.PassContext
    ) -> tvm.ir.IRModule:
        for global_var, func in mod.functions.items():
            func = rewrite(MatchOnnxDigitalRequantRewriter(), func)
            mod.update_func(global_var, func)
        return mod

    def __call__(self, mod):
        return self.transform_module(mod)


@transform.function_pass(opt_level=0)
class MatchOnnxIntegerize(ExprMutator):
    """Cast linear layers in graph to integers and insert the necessary cast operations (from MATCH ONNX file)
    """

    def __init__(self, dtype):
        self.dtype = dtype
        super().__init__()

    def transform_function(self, func, mod, ctx):
        return self.visit(func)

    def visit_call(self, call):
        """Rewrite ops
        """
        new_fn = self.visit(call.op)
        new_args = [self.visit(arg) for arg in call.args]
        # Default case
        new_call = relay.Call(new_fn, new_args, call.attrs, call.type_args, call.span)

        if call.op.name == 'nn.conv2d' and new_args[1].data.dtype.startswith('int'):
            # ensure that the output of the conv2d op is int32
            w = new_args[1]
            new_call = relay.op.nn.conv2d(new_args[0], w,
                                          strides=call.attrs.strides,
                                          padding=call.attrs.padding,
                                          dilation=call.attrs.dilation,
                                          groups=call.attrs.groups,
                                          out_dtype='int32',
                                          kernel_size=w.data.shape[-2:])

        elif call.op.name == 'nn.dense' and new_args[1].data.dtype.startswith('int'):
            # ensure that the output of the dense op is int32
            new_call = relay.op.nn.dense(new_args[0], new_args[1], out_dtype='int32')

        elif call.op.name == 'nn.bias_add' or call.op.name == 'add':
            # ensure bias data type matches the data type of previous operation's output type
            # make sure to eliminate element-wise add, so check if rhs is constant
            new_call = relay.Call(new_fn, new_args, call.attrs, call.type_args, call.span)
            if isinstance(new_args[1], relay.Constant):
                dtype = new_args[0].attrs.out_dtype if new_args[0].attrs is not None else "int32"
                new_args[1] = relay.const(new_args[1].data.numpy().astype(dtype))
                new_call = relay.Call(new_fn, new_args, call.attrs, call.type_args, call.span)

        elif call.op.name == 'multiply' or call.op.name == "add":
            new_call = relay.Call(new_fn, new_args, call.attrs, call.type_args, call.span)
            if isinstance(new_args[1], relay.Constant):
                dtype = new_args[0].attrs.out_dtype if new_args[0].attrs is not None else "int32"
                new_args[1] = relay.const(new_args[1].data.numpy().astype(dtype))
                new_call = relay.Call(new_fn, new_args, call.attrs, call.type_args, call.span)

        elif call.op.name == 'divide':
            # a divide operation with division factor > 1 that is a power of two, is assumed to be a dequant op
            # put cast before this op in that case
            x = new_args[0]
            div = new_args[1].data.numpy().item()
            if div >= 1 and np.log2(div).is_integer():
                x = relay.cast(x, 'float')
            new_call = relay.divide(x, new_args[1])

        elif call.op.name == 'minimum':
            # test if this is the last layer of the quantize sequence, if so, put cast after this op
            new_call = relay.minimum(new_args[0], new_args[1])
            if new_args[0].op.name == "maximum" and \
               new_args[0].args[0].op.name == "floor" and \
               new_args[0].args[0].args[0].op.name == "divide":
                new_call = relay.cast(new_call, self.dtype)

        return new_call

    def visit_function(self, fn):
        """Rewrite function arguments
        """
        new_params = []
        binds = {}

        for param in fn.params:
            # Get the parameter's type annotation.
            var_type = param.type_annotation

            # bias params are int32
            if param.name_hint.endswith('bias'):
                dtype = 'int32'
            else:
                dtype = self.dtype

            # Generate new variable.
            new_param = relay.var(param.name_hint, shape=var_type.shape, dtype=dtype)

            new_params.append(new_param)
            binds[param] = new_param

        new_body = self.visit(fn.body)
        # Rewrite the body to use new parameters.
        new_body = relay.bind(new_body, binds)

        # Construct the updated function and return.
        return relay.Function(
            new_params,
            new_body,
            # You could change the return type, if you use None it will re-infer.
            None,
            type_params=fn.type_params,
            attrs=fn.attrs,
        )


class FindLayoutTransformShape(ExprVisitor):
    """Convert relay graph to dory graph
    """
    def __init__(self):
        super().__init__()
        self.shapes = []

    def visit_call(self, call):
        """Extract parameters and construct dory graph"""
        self.visit(call.op)
        for a in call.args:
            self.visit(a)

        if isinstance(call.op, tvm.ir.Op) and not isinstance(call.args[0], relay.Constant):
            # we don't want to insert transformations on constants like weights and biases
            if call.op.name == 'annotation.compiler_begin' and call.attrs.compiler == 'match':
                self.shapes.append(call.args[0].checked_type.shape)

            elif call.op.name == 'annotation.compiler_end' and call.attrs.compiler == 'match':
                self.shapes.append(call.args[0].checked_type.shape)