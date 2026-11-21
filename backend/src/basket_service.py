from datetime import datetime,timezone
from sqlalchemy import select
from fastapi import HTTPException
from .models import HistoricalBasket,BasketWithdrawal,Pack
from .common import add,audit
from . import analysis_service as analysis

def _lock_basket(s,basket_id):
    b=s.scalar(select(HistoricalBasket).where(HistoricalBasket.id==basket_id).with_for_update().execution_options(populate_existing=True))
    if not b:raise HTTPException(404,'历史课程篮不存在')
    return b

def withdraw(s,basket_id,d,u):
    """撤回一份历史课程篮。
    幂等：已撤回时重复请求不重复登记，直接返回原登记。
    并发安全：先锁篮（与 worker 的加锁顺序一致，避免死锁），登记后再处理各 Pack；
    worker 写回前在同一 Pack 行锁内复查篮状态，晚到的分析结果不会复活撤回明细。"""
    b=_lock_basket(s,basket_id)
    if b.state=='withdrawn':
        w=s.scalar(select(BasketWithdrawal).where(BasketWithdrawal.basket_id==basket_id))
        audit(s,u,'撤回重试，确认登记已存在 '+b.code)
        return {'basket_id':basket_id,'code':b.code,'state':b.state,'withdrawal':w and {'id':w.id,'reason':w.reason,'actor':w.actor},'idempotent':True,'affected_pack_ids':w.affected_pack_ids if w else []}
    if s.scalar(select(BasketWithdrawal.id).where(BasketWithdrawal.basket_id==basket_id)):
        raise HTTPException(409,'该篮已有撤回登记，状态异常，请人工核对')
    registration=add(s,BasketWithdrawal(basket_id=basket_id,reason=d.reason,actor=u.email,affected_pack_ids=[],created_at=datetime.now(timezone.utc).isoformat(timespec='seconds')))
    b.state='withdrawn';b.withdrawal_id=registration['id']
    s.flush()
    # 先登记后处理：任何并发分析此刻都会在篮锁/复查处看到 withdrawn
    affected=analysis.invalidate_for_withdrawal(s,basket_id)
    # 排队中的任务无需作废：worker 取到后只会用在档篮重算；运行中的任务写回时会被复查拦下
    w=s.get(BasketWithdrawal,registration['id'])
    w.affected_pack_ids=affected
    audit(s,u,'撤回历史课程篮 %s，失效分析 %s 份'% (b.code,len(affected)))
    s.flush()
    return {'basket_id':basket_id,'code':b.code,'state':b.state,'withdrawal':registration,'idempotent':False,'affected_pack_ids':affected}
