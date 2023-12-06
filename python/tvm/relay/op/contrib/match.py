def partition_for_match(mod, params=None, dpu=None, **opts):
    try:
        #breakpoint()
        from match import partition
        return partition(mod,params,dpu,opts)
    except ImportError:
        raise ImportError("Match not detected correctly in your system!")
