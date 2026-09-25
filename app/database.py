from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from .config import settings


def configure_sqlite_concurrency(engine):
    """SQLite 写事务改用 BEGIN IMMEDIATE。

    默认的延迟事务在「先读后写」的并发场景下会出现 SHARED→RESERVED
    锁升级死锁；改为 IMMEDIATE 后，并发写事务在起点排队（等待方在
    busy timeout 内重试），确认容量预约这类「校验+落账」的复合操作
    得以在一笔事务内串行完成。
    """
    if engine.url.get_backend_name() != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def _sqlite_connect(dbapi_connection, _connection_record):
        # 关闭 pysqlite 自动 BEGIN，由 SQLAlchemy 统一控制事务
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _sqlite_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")


engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},
)
configure_sqlite_concurrency(engine)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
