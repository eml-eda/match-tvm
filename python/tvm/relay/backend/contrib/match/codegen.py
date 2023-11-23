import tvm
from . import _ffi_api
@tvm._ffi.register_func("relay.ext.match")
def byoc_compiler(mod: tvm.ir.IRModule):
    try:
        import match
        code=match.codegen.codegen(mod)
        func_name = mod.attrs.global_symbol
        syms = [func_name]
        return _ffi_api.CSourceModuleCreate(code, "c", syms, [])
    except ImportError:
        raise ImportError("Match not detected correctly in your system!")