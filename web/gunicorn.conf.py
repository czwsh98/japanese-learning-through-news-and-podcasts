def post_worker_init(_worker):
    """Run crash recovery only for a real Gunicorn worker startup."""
    from web.db import reconcile_stale_usage, reconcile_worker_restart

    reconcile_worker_restart()
    reconcile_stale_usage()
