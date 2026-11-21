import os
from sqlalchemy import select
from src.db import Base
from src import models
from src.auth import hashed
from helpers import engine,TestSession

class FakeRedis:
    def __init__(self):self.items=[]
    def rpush(self,k,v):self.items.append((k,v))
    def ping(self):return True
import src.main as main
main.q=FakeRedis()
from fastapi.testclient import TestClient
client=TestClient(main.app)

def setup_module():
    Base.metadata.create_all(engine)

def setup_module():
    Base.metadata.create_all(engine)
    s=TestSession()
    for role in('operator','supervisor','viewer'):
        s.add(models.User(email=role+'@xunshu.demo',password_hash=hashed('Books2026!'),role=role))
    patterns=[['math','physics','lab'],['language','history'],['math','computing'],['art','music']]
    for i in range(96):
        s.add(models.HistoricalBasket(code='HIST-%03d'%i,subjects=patterns[i%4]))
    room=models.Classroom(code='G1-1',name='一班',size=12);s.add(room);s.flush()
    e1=models.Edition(code='PHY-V2',title='物理',subject='physics',version='V2',deposit_cents=2000)
    e2=models.Edition(code='LAB-V2',title='实验',subject='lab',version='V2',deposit_cents=2000)
    s.add_all([e1,e2]);s.flush()
    p=models.Pack(code='PACK-T',classroom_id=room.id,term='2026秋',ml_state='ready')
    s.add(p);s.flush()
    s.add_all([models.PackLine(pack_id=p.id,edition_id=e1.id,quantity=1),models.PackLine(pack_id=p.id,edition_id=e2.id,quantity=1)])
    s.commit();s.close()

def token(role):
    r=client.post('/auth/login',json={'email':role+'@xunshu.demo','password':'Books2026!'})
    return r.json()['token']
def auth(role):return {'Authorization':'Bearer '+token(role)}

def test_archive_includes_baskets_and_withdrawals():
    r=client.get('/archive',headers=auth('viewer'))
    assert r.status_code==200
    body=r.json()
    assert len(body['historical_baskets'])==96
    assert body['basket_withdrawals']==[]
    assert all('recommendation_status' in p for p in body['packs'])

def test_viewer_cannot_withdraw():
    bid=TestSession().scalar(select(models.HistoricalBasket.id).limit(1))
    r=client.post('/historical-baskets/%d/withdraw'%bid,json={'reason':'查阅席无权操作'},headers=auth('viewer'))
    assert r.status_code==403

def test_supervisor_withdraw_end_to_end_and_idempotent():
    s=TestSession();bid=s.scalar(select(models.HistoricalBasket.id).where(models.HistoricalBasket.code=='HIST-010'));s.close()
    payload={'reason':'教师反馈该历史清单与实际课程不符'}
    r1=client.post('/historical-baskets/%d/withdraw'%bid,json=payload,headers=auth('supervisor'))
    assert r1.status_code==200,r1.text
    body=r1.json()
    assert body['state']=='withdrawn' and body['idempotent']is False
    # 撤回后训练/留出不再含该篮
    s=TestSession()
    from src import analysis_service as analysis
    pack_id=s.scalar(select(models.Pack.id).limit(1))
    result,used=analysis.run_basket_analysis(s,pack_id)
    assert bid not in used and bid not in result['basket_ids']
    s.close()
    # 重试不重复登记
    r2=client.post('/historical-baskets/%d/withdraw'%bid,json=payload,headers=auth('supervisor'))
    assert r2.status_code==200 and r2.json()['idempotent']is True
    s=TestSession()
    assert s.query(models.BasketWithdrawal).filter_by(basket_id=bid).count()==1
    assert s.get(models.HistoricalBasket,bid).state=='withdrawn'
    s.close()

def test_withdraw_unknown_basket_404():
    r=client.post('/historical-baskets/999999/withdraw',json={'reason':'不存在的篮编号'},headers=auth('supervisor'))
    assert r.status_code==404

def test_recommend_endpoint_allows_stale_requeue():
    s=TestSession();p=s.scalar(select(models.Pack));p.ml_state='stale';pid=p.id;s.commit();s.close()
    r=client.post('/packs/%d/recommend'%pid,headers=auth('operator'))
    assert r.status_code==200 and r.json()['ml_state']=='queued'
    assert main.q.items and main.q.items[-1][1]==pid
