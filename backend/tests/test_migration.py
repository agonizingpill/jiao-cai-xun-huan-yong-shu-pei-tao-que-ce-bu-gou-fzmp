import os,json
from sqlalchemy import create_engine,text,inspect
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker
import src.db as db
from src.seed_service import _migrate

def fresh_engine():
    eng=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    return eng

def test_migration_adds_columns_and_marks_legacy_review():
    engine=fresh_engine()
    with engine.begin() as c:
        c.execute(text('CREATE TABLE packs(id INTEGER PRIMARY KEY,code TEXT,classroom_id INTEGER,term TEXT,state TEXT,ml_state TEXT,recommendation JSON,error TEXT)'))
        c.execute(text('CREATE TABLE historical_baskets(id INTEGER PRIMARY KEY,code TEXT,subjects JSON)'))
        # 旧分析：没有 basket_ids 也没有任何 indices 血缘
        legacy=json.dumps({'rules':[],'suggestions':[],'holdout_cases':[]})
        c.execute(text("INSERT INTO packs(id,code,classroom_id,term,state,ml_state,recommendation) VALUES(1,'P1',1,'t','planned','ready',:r)"),{'r':legacy})
        c.execute(text("INSERT INTO historical_baskets(id,code,subjects) VALUES(1,'H1','[\"math\"]')"))
    _migrate(engine)
    pcols={c['name']for c in inspect(engine).get_columns('packs')}
    bcols={c['name']for c in inspect(engine).get_columns('historical_baskets')}
    assert {'recommendation_status','review_note'}<=pcols
    assert {'state','withdrawal_id'}<=bcols
    with engine.begin() as c:
        status,note=c.execute(text('SELECT recommendation_status,review_note FROM packs WHERE id=1')).first()
        bstate=c.execute(text('SELECT state FROM historical_baskets WHERE id=1')).first()[0]
    assert status=='review' and '待人工核验' in note
    assert bstate=='active'
    # 幂等：再跑一次不报错、不重复加列
    _migrate(engine)

def test_migration_keeps_lineaged_recommendation_active():
    eng=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    with eng.begin() as c:
        c.execute(text('CREATE TABLE packs(id INTEGER PRIMARY KEY,code TEXT,classroom_id INTEGER,term TEXT,state TEXT,ml_state TEXT,recommendation JSON,error TEXT)'))
        c.execute(text('CREATE TABLE historical_baskets(id INTEGER PRIMARY KEY,code TEXT,subjects JSON)'))
        with_lineage=json.dumps({'basket_ids':[1,2],'train_indices':[0],'holdout_indices':[1],'holdout_cases':[]})
        c.execute(text("INSERT INTO packs(id,code,classroom_id,term,state,ml_state,recommendation) VALUES(2,'P2',1,'t','planned','ready',:r)"),{'r':with_lineage})
    _migrate(eng)
    with eng.begin() as c:
        status=c.execute(text('SELECT recommendation_status FROM packs WHERE id=2')).first()[0]
    assert status=='active'
