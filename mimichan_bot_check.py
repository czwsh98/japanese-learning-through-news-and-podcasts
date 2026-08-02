import json, sys
sys.path.insert(0, '/root/mimichan')
from mimichan_bot import do_digest, fetch_all_recent, refresh_recent_cache, load_config
cfg = load_config()
owner_email = cfg.get('owner_email', 'czwsimon@gmail.com')
sources, recent_by_source = fetch_all_recent()
result = do_digest(cfg['token'], cfg['chat_id'], owner_email,
                   sources=sources, recent_by_source=recent_by_source)
print(result)
# Refresh the /subscriptions page's recent-episodes cache (best-effort).
refresh_recent_cache(owner_email, sources=sources, recent_by_source=recent_by_source)
