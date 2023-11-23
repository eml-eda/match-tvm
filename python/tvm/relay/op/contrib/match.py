def partition_for_match(mod, params=None, dpu=None, **opts):
    try:
        import match
        return match.partition.partition(mod,params,dpu,opts)
    except ImportError:
        raise ImportError("Match not detected correctly in your system!")