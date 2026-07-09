import json, sys
sys.path.insert(0, '/root/mimichan')
from mimichan_bot import do_check, load_config
cfg = load_config()
result = do_check(cfg['token'], cfg['chat_id'])
print(result)
