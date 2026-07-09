import json, sys
sys.path.insert(0, '/root/mimichan')
from mimichan_bot import do_check, refresh_recent_cache, load_config
cfg = load_config()
result = do_check(cfg['token'], cfg['chat_id'])
print(result)
# Refresh the /subscriptions page's recent-episodes cache (best-effort).
refresh_recent_cache(cfg.get('owner_email', 'czwsimon@gmail.com'))
