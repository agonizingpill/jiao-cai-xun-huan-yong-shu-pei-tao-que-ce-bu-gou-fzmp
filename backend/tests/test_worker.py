import os,json,tempfile
import random
from sqlalchemy import select
from src.db import Base
from src import models
from src import worker
from src import analysis_service as analysis
from src import basket_service
from src.schemas import BasketWithdrawalInput
from helpers import engine
from sqlalchemy.orm import sessionmaker
S=sessionmaker(engine,expire_on_commit=False)

class FakeRedis:
    def __init__(self):self.pushed=[]
    def rpush(self,k,v):self.pushed.append((k,int(v)))

class U:
    def __init__(self,e):self.email=e
SUP=U('supervisor@xunshu.demo')

def setup_module():Base.metadata.create_all(engine)

def seed(n=96):
    s=S()
    for m in(models.BasketWithdrawal,models.HistoricalBasket,models.Pack):s.query(m).delete()
    s.commit()
    pats=[['math','physics','lab'],['language','history'],['math','computing'],['art','music']]
    for i in range(n):s.add(models.HistoricalBasket(code='W-%03d-%d'%(i,random.randint(0,999999)),subjects=pats[i%4]))
    room=models.Classroom(code='R-%d'%random.randint(0,10**6),name='班',size=12);s.add(room);s.flush()
    p=models.Pack(code='PK-%d'%random.randint(0,10**6),classroom_id=room.id,term='2026秋',ml_state='queued');s.add(p);s.flush()
    s.commit();pid=p.id;s.close();return pid

def test_handle_pack_writes_ready_result_and_artifact():
    pid=seed();fr=FakeRedis()
    with tempfile.TemporaryDirectory()as artifacts:
        with S()as s:
            out=worker.handle_pack(s,pid,fr,artifact_dir=artifacts)
        assert out=='ready' and fr.pushed==[]
        p=S().get(models.Pack,pid)
        assert p.ml_state=='ready' and p.recommendation_status=='active'
        assert 'basket_ids' in p.recommendation and len(p.recommendation['holdout_cases'])==24
        assert os.path.exists(os.path.join(artifacts,'basket-%d.json'%pid))
        assert json.load(open(os.path.join(artifacts,'basket-%d.json'%pid)))['basket_ids']

def test_handle_pack_requeues_when_snapshot_stale_and_does_not_write():
    pid=seed();fr=FakeRedis()
    orig=analysis.snapshot_stale
    analysis.snapshot_stale=lambda s,ids:True
    try:
        with tempfile.TemporaryDirectory()as artifacts:
            with S()as s:
                out=worker.handle_pack(s,pid,fr,artifact_dir=artifacts)
            assert out=='requeued-stale'
            assert ('book-baskets',pid) in fr.pushed
            p=S().get(models.Pack,pid)
            assert p.ml_state=='queued' and p.recommendation is None
            assert not os.path.exists(os.path.join(artifacts,'basket-%d.json'%pid))
    finally:analysis.snapshot_stale=orig

def test_handle_pack_marks_failed_on_compute_error():
    pid=seed()
    s=S();ids=list(s.scalars(select(models.HistoricalBasket.id).order_by(models.HistoricalBasket.id)))
    # 撤回 30 份使在档篮低于 73，recommend 抛错
    for bid in ids[:30]:
        basket_service.withdraw(s,bid,BasketWithdrawalInput(reason='批量撤回压测语料'),SUP)
    s.commit()
    with tempfile.TemporaryDirectory()as artifacts:
        with S()as s2:
            out=worker.handle_pack(s2,pid,FakeRedis(),artifact_dir=artifacts)
    assert out=='failed'
    p=S().get(models.Pack,pid)
    assert p.ml_state=='failed' and p.recommendation is None and p.error

def test_handle_pack_ignores_non_queued_message():
    pid=seed();s=S();p=s.get(models.Pack,pid);p.ml_state='ready';s.commit();s.close()
    with S()as sx:
        assert worker.handle_pack(sx,pid,FakeRedis())=='skip-not-queued'
