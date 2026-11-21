import os
os.environ.setdefault('DATABASE_URL','sqlite://')
os.environ.setdefault('REDIS_URL','redis://localhost:6379/0')
os.environ.setdefault('JWT_SECRET','test-secret')
os.environ['SEED_DEMO']='false'
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
import src.db as db

# 全部测试共用一个进程内 SQLite 库（StaticPool 单连接），并把应用指向它
engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
TestSession=sessionmaker(engine,expire_on_commit=False)
db.engine=engine
db.Session=TestSession
