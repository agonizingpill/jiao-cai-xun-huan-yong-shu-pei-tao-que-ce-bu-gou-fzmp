import os,time,json,logging
from pathlib import Path
import redis
from .db import Session
from .models import Pack
from .common import lock
from . import analysis_service as analysis
q=redis.Redis.from_url(os.environ['REDIS_URL'],decode_responses=True)

def handle_pack(s,pack_id,queue,artifact_dir='/artifacts'):
    """处理一条分析消息。返回处理结果说明，便于测试与日志。"""
    p=lock(s,Pack,pack_id)
    if p.ml_state!='queued':return 'skip-not-queued'
    p.ml_state='running';s.commit()
    try:
        # 计算只读取在档篮；随后在同一 Pack 行锁内复查，杜绝撤回后的晚到写回
        result,basket_ids=analysis.run_basket_analysis(s,p.id)
        p=lock(s,Pack,p.id)
        if p.ml_state!='running':
            logging.info('pack %s 分析写回前状态已变为 %s，丢弃本次结果',p.id,p.ml_state);return 'skip-state-'+p.ml_state
        if analysis.snapshot_stale(s,basket_ids):
            # 撤回与本分析并发：本结果基于的篮集已过期，丢弃并回到排队，由下一轮只用在档篮重算
            p.ml_state='queued';p.error='历史篮在分析期间发生撤回，已重新排队';s.commit();queue.rpush('book-baskets',p.id);return 'requeued-stale'
        p.recommendation=result;p.recommendation_status='active';p.review_note='';p.ml_state='ready';p.error=None
        Path(f'{artifact_dir}/basket-{p.id}.json').write_text(json.dumps(result,ensure_ascii=False))
        s.commit();return 'ready'
    except Exception as ex:
        p=lock(s,Pack,p.id)
        if p.ml_state=='running':p.ml_state='failed';p.error=str(ex)[:450];s.commit()
        logging.exception('basket failed');return 'failed'

def main():
    while True:
        Path('/tmp/books-heartbeat').touch()
        try:
            item=q.blpop('book-baskets',timeout=2)
            if not item:continue
            with Session()as s:handle_pack(s,int(item[1]),q)
        except Exception:logging.exception('queue unavailable');time.sleep(1)

if __name__=='__main__':main()
