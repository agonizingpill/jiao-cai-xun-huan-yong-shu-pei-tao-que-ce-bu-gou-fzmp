from sqlalchemy import select
from .models import Pack,PackLine,Edition,HistoricalBasket
from .constants import RECOMMENDATION_STATES
from .basket_rules import recommend

ACTIVE=RECOMMENDATION_STATES[0]
INVALID=RECOMMENDATION_STATES[1]
REVIEW=RECOMMENDATION_STATES[2]

def active_baskets(s):
    """训练与留出只取在档（未撤回）历史篮，按稳定 id 顺序返回 (id, subjects)。"""
    rows=s.execute(select(HistoricalBasket.id,HistoricalBasket.subjects).where(HistoricalBasket.state=='active').order_by(HistoricalBasket.id)).all()
    return [(row.id,list(row.subjects)) for row in rows]

def pack_subjects(s,pack_id):
    return list(s.scalars(select(Edition.subject).join(PackLine,PackLine.edition_id==Edition.id).where(PackLine.pack_id==pack_id)))

def run_basket_analysis(s,pack_id):
    """只用在档篮重算 Apriori；结果中的索引/逐篮明细均指向在档篮，撤掉的篮不会出现。
    返回 (结果, 本次使用的在档篮 id 快照)，供写回前复查并发撤回。"""
    baskets=active_baskets(s)
    if len(baskets)<=72:raise ValueError('在档历史篮不足 73 份，无法保持固定 72 训练/留出口径，请补足语料后再分析')
    result=recommend([subjects for _,subjects in baskets],pack_subjects(s,pack_id))
    # recommend 返回的是在档序列上的位置，映射回稳定篮 id，避免撤回后位置错位
    ids=[bid for bid,_ in baskets]
    result['basket_ids']=ids
    result['train_basket_ids']=[ids[i]for i in result.pop('train_indices')]
    result['holdout_basket_ids']=[ids[i]for i in result.pop('holdout_indices')]
    for case in result['holdout_cases']:
        pos=case.pop('basket_index')
        case['basket_id']=ids[pos]
    return result,ids

def _lineage_basket_ids(rec):
    """旧分析可能没有 basket 血缘字段；返回 None 表示认不出用了哪份篮。"""
    if not isinstance(rec,dict):return None
    if rec.get('basket_ids') is not None:return list(rec.get('basket_ids') or [])
    legacy=list(rec.get('train_indices') or [])+list(rec.get('holdout_indices') or [])
    return legacy or None

def classify_recommendation(rec,withdrawn_ids):
    """判定一份已发出的分析在撤回后的状态。
    active：血缘清晰且未用到撤掉的篮；
    invalid：逐篮明细或训练/留出索引里出现了撤掉的篮；
    review：旧分析没有篮血缘，无法确认，交人工待核。"""
    lineage=_lineage_basket_ids(rec)
    if lineage is None:return REVIEW,'旧分析缺少篮血缘，无法确认是否使用过撤回篮，待人工核验'
    hit=sorted(set(lineage)&set(withdrawn_ids))
    if hit:return INVALID,'分析曾使用已撤回的历史篮（id %s），建议与逐篮明细失效'%','.join(map(str,hit))
    return ACTIVE,''

def purge_withdrawn_cases(rec,withdrawn_ids):
    """清掉分析结果中属于撤回篮的逐篮留出明细，其余篮照查；返回 (新结果, 清掉的明细数)。"""
    if not isinstance(rec,dict) or 'holdout_cases' not in rec:return rec,0
    kept=[];removed=0
    for case in rec['holdout_cases']:
        bid=case.get('basket_id',case.get('basket_index'))
        if bid in withdrawn_ids:removed+=1
        else:kept.append(case)
    rec=dict(rec);rec['holdout_cases']=kept
    rec['holdout_rows']=len(kept)
    return rec,removed

def snapshot_stale(s,basket_ids):
    """写回前复查：本次分析使用的篮快照里，是否有篮在计算期间被撤回。"""
    if not basket_ids:return True
    return s.scalar(select(HistoricalBasket.id).where(HistoricalBasket.id.in_(basket_ids),HistoricalBasket.state!='active').limit(1)) is not None

def invalidate_for_withdrawal(s,withdrawn_basket_id):
    """撤回登记后处理全部既有分析：
    用过该篮的标失效并清逐篮明细；认不出血缘的标待核；其余保持在档可查。
    返回受影响的 pack id 列表。"""
    affected=[]
    packs=list(s.scalars(select(Pack).where(Pack.recommendation.is_not(None)).order_by(Pack.id)))
    for p in packs:
        status,note=classify_recommendation(p.recommendation,[withdrawn_basket_id])
        if status==ACTIVE:continue
        cleaned,removed=purge_withdrawn_cases(p.recommendation,[withdrawn_basket_id])
        p.recommendation=cleaned
        p.recommendation_status=status
        p.review_note=note
        if status==INVALID:
            p.ml_state='stale'
            affected.append(p.id)
    return affected
