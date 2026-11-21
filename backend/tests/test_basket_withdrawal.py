import os
import random
from sqlalchemy import select,func
from src.db import Base
from src import models
from src import analysis_service as analysis
from src import basket_service
from src.schemas import BasketWithdrawalInput
from fastapi import HTTPException
from helpers import engine
from sqlalchemy.orm import sessionmaker
Session=sessionmaker(engine,expire_on_commit=False)

class U:
    def __init__(self,email):self.email=email
SUP=U('supervisor@xunshu.demo')

def setup_module():
    Base.metadata.create_all(engine)

def fresh():return Session()

def seed_baskets(n=96):
    s=Session()
    for m in (models.BasketWithdrawal,models.HistoricalBasket,models.Pack):
        s.query(m).delete()
    s.commit()
    patterns=[['math','physics','lab'],['language','history'],['math','computing'],['art','music']]
    for i in range(n):
        s.add(models.HistoricalBasket(code='H-%03d-%d'%(i,random.randint(0,99999)),subjects=patterns[i%4]))
    s.commit()
    ids=list(s.scalars(select(models.HistoricalBasket.id).order_by(models.HistoricalBasket.id)))
    s.close();return ids

def make_pack(s,with_rec=None):
    p=models.Pack(code='PACK-%d'%random.randint(0,10**9),classroom_id=1,term='2026秋',ml_state='ready')
    if with_rec is not None:p.recommendation=with_rec;p.recommendation_status='active'
    s.add(p);s.flush();return p

def mark_withdrawn(bid,reason='r'):
    s=Session()
    b=s.get(models.HistoricalBasket,bid);b.state='withdrawn'
    s.add(models.BasketWithdrawal(basket_id=bid,reason=reason,actor='a',affected_pack_ids=[],created_at='2026-01-01T00:00:00+00:00'))
    s.commit();s.close()

# 1. 撤回后训练/留出不再使用该篮，且结果中完全不出现该篮 id
def test_withdrawn_basket_excluded_from_train_and_holdout():
    ids=seed_baskets(96);target=ids[3];s=fresh()
    mark_withdrawn(target)
    result,used=analysis.run_basket_analysis(s,99)
    assert target not in used
    assert target not in result['basket_ids']
    assert target not in result['train_basket_ids']
    assert target not in result['holdout_basket_ids']
    assert all(c['basket_id']!=target for c in result['holdout_cases'])
    assert result['train_rows']+result['holdout_rows']==95
    s.close()

# 2. 撤回清掉历次分析中该篮的逐篮明细，其余篮照查
def test_purge_removes_only_withdrawn_case():
    ids=seed_baskets(20);s=fresh()
    rec={'holdout_cases':[{'basket_id':ids[0],'hidden':'math','visible':['physics'],'recommended':['math'],'hit':True},
                          {'basket_id':ids[1],'hidden':'art','visible':['music'],'recommended':[],'hit':False},
                          {'basket_id':ids[2],'hidden':'lab','visible':['math'],'recommended':['lab'],'hit':True}]}
    cleaned,removed=analysis.purge_withdrawn_cases(rec,[ids[1]])
    assert removed==1
    assert [c['basket_id']for c in cleaned['holdout_cases']]==[ids[0],ids[2]]
    assert cleaned['holdout_rows']==2
    s.close()

# 3. 用过撤回篮的分析标失效；没用到的保持有效
def test_invalidation_classifies_used_vs_unused():
    ids=seed_baskets(20);target=ids[5];s=fresh()
    used_rec={'basket_ids':ids,'holdout_cases':[{'basket_id':i}for i in ids[:8]]}
    clean_rec={'basket_ids':[i for i in ids if i!=target],'holdout_cases':[{'basket_id':ids[0]}]}
    p1=make_pack(s,used_rec);p2=make_pack(s,clean_rec);s.commit()
    affected=analysis.invalidate_for_withdrawal(s,target);s.commit()
    assert p1.id in affected and p2.id not in affected
    s.expire_all()
    assert s.get(models.Pack,p1.id).recommendation_status=='invalid'
    assert s.get(models.Pack,p1.id).ml_state=='stale'
    assert all(c['basket_id']!=target for c in s.get(models.Pack,p1.id).recommendation['holdout_cases'])
    assert s.get(models.Pack,p2.id).recommendation_status=='active'
    s.close()

# 4. 旧分析认不出血缘 -> 待核
def test_legacy_recommendation_without_lineage_marks_review():
    seed_baskets(20);s=fresh()
    legacy={'rules':[],'suggestions':[],'holdout_cases':[]}
    status,note=analysis.classify_recommendation(legacy,[1])
    assert status=='review' and note
    p=make_pack(s,legacy);s.commit()
    analysis.invalidate_for_withdrawal(s,1);s.commit()
    s.expire_all()
    assert s.get(models.Pack,p.id).recommendation_status=='review'
    assert s.get(models.Pack,p.id).ml_state!='stale'
    s.close()

# 5. 撤回重试不重复登记（幂等）
def test_withdraw_idempotent_no_duplicate_registration():
    ids=seed_baskets(20);target=ids[2];s=fresh()
    r1=basket_service.withdraw(s,target,BasketWithdrawalInput(reason='教师反馈有误'),SUP);s.commit()
    assert r1['idempotent']is False
    assert s.scalar(select(func.count()).select_from(models.BasketWithdrawal).where(models.BasketWithdrawal.basket_id==target))==1
    r2=basket_service.withdraw(s,target,BasketWithdrawalInput(reason='再点一次'),SUP);s.commit()
    assert r2['idempotent']is True
    assert s.scalar(select(func.count()).select_from(models.BasketWithdrawal).where(models.BasketWithdrawal.basket_id==target))==1
    assert s.get(models.HistoricalBasket,target).state=='withdrawn'
    s.close()

# 6. 撤回不存在的篮 -> 404
def test_withdraw_missing_basket_404():
    seed_baskets(5);s=fresh()
    try:
        basket_service.withdraw(s,999999,BasketWithdrawalInput(reason='不存在的篮'),SUP);assert False
    except HTTPException as e:assert e.status_code==404
    s.close()

# 7. 并发：分析进行中发生撤回，晚到结果不能写回（模拟 worker 快照复查）
def test_late_analysis_result_rejected_after_concurrent_withdrawal():
    ids=seed_baskets(96);s=fresh()
    p=make_pack(s);p.ml_state='running';s.commit();pid=p.id
    result,snapshot=analysis.run_basket_analysis(s,pid)
    s2=Session()
    basket_service.withdraw(s2,ids[4],BasketWithdrawalInput(reason='并发撤回'),SUP);s2.commit();s2.close()
    assert analysis.snapshot_stale(s,snapshot) is True
    s.expire_all()
    dbp=s.get(models.Pack,pid)
    assert dbp.recommendation is None
    assert dbp.ml_state!='ready'
    s.close()

# 8. 无撤回时快照新鲜，正常写回
def test_snapshot_fresh_when_no_withdrawal():
    seed_baskets(96);s=fresh()
    _,snapshot=analysis.run_basket_analysis(s,1)
    assert analysis.snapshot_stale(s,snapshot) is False
    s.close()

# 8b. 撤回写审计；未使用该篮的分析保持在档可查，使用过的失效且明细清除
def test_withdrawal_audits_and_keeps_unaffected_packs_queryable():
    ids=seed_baskets(96);target=ids[10];s=fresh()
    affected_pack=make_pack(s,{'basket_ids':list(ids),'holdout_cases':[{'basket_id':target},{'basket_id':ids[0]}]})
    other_pack=make_pack(s,{'basket_ids':[i for i in ids if i!=target],'holdout_cases':[{'basket_id':ids[0]}]})
    s.commit()
    basket_service.withdraw(s,target,BasketWithdrawalInput(reason='审计留痕撤回测试'),SUP);s.commit()
    audits=list(s.scalars(select(models.Audit).where(models.Audit.actor==SUP.email)))
    assert any('撤回历史课程篮'in a.action for a in audits)
    s.expire_all()
    ap=s.get(models.Pack,affected_pack.id);op=s.get(models.Pack,other_pack.id)
    assert ap.recommendation_status=='invalid' and ap.ml_state=='stale'
    assert all(c['basket_id']!=target for c in ap.recommendation['holdout_cases'])
    # 其余分析照查：状态仍有效，明细保留
    assert op.recommendation_status=='active' and op.ml_state=='ready'
    assert len(op.recommendation['holdout_cases'])==1
    s.close()

# 9. 映射回稳定 id：篮集变化后结果索引不再错位
def test_indices_map_to_stable_basket_ids():
    ids=seed_baskets(96);s=fresh()
    mark_withdrawn(ids[0])
    result,_=analysis.run_basket_analysis(s,1)
    assert set(result['train_basket_ids']).isdisjoint([ids[0]])
    assert isinstance(result['holdout_cases'][0]['basket_id'],int)
    assert 'basket_index' not in result['holdout_cases'][0]
    s.close()

# 10. 在档篮不足 73 份时报错，不产生残缺分析（撤回过多会触发）
def test_insufficient_baskets_raises():
    seed_baskets(70);s=fresh()
    try:
        analysis.run_basket_analysis(s,1);assert False
    except ValueError:pass
    s.close()
