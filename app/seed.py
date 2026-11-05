"""字典与基础数据初始化（幂等）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import MaterialDict, Period, ProcessDict, ProductDict, Role, Worker, WorkerRole

# 角色：kind 决定该角色的人进入哪份识别名册
#   worker    -> 「工作者」列
#   inspector -> 「检验员/检查员」列
#   receiver  -> 「接交人/交接人」列
#   other     -> 仅用于人员归类，不参与识别
ROLES: list[tuple[str, str, str, str]] = [
    ("WORKER", "工作者", "worker", "干活的人，明细「操作者」列按此名册匹配"),
    ("INSPECTOR", "检验员", "inspector", "明细「检查员/检验员」列按此名册匹配"),
    ("RECEIVER", "接交人", "receiver", "明细「接交人/交接人」列按此名册匹配"),
    ("DISPATCHER", "计调员", "other", "表头「计调员」栏"),
    ("PICKER", "材料员", "other", "表头「材料员」栏"),
]

# 来源：doc/9月2车间工时核算.xlsx「总工时」sheet
WORKERS: list[tuple[str, str, str]] = [
    ("陈永安", "镗", ""),
    ("万庆泓", "铣", ""),
    ("孙建成", "立加", ""),
    ("崔建安", "铣", ""),
    ("王国良", "车", "刨"),
    ("袁红华", "钳", ""),
    ("段小明", "车工", ""),
    ("段爱国", "车", "磨,精磨"),
    ("宋国良", "车工", ""),
    ("孙广智", "车工", ""),
    ("白小峰", "线", "折弯"),
    ("梁继斌", "数车", "磨"),
    ("马永刚", "电工", ""),
]

INSPECTORS = ["杜文杰", "符媛媛", "陈志刚"]

# 产品字典：name, code(产品代号), aliases, spec
# name 用单据上实际写的代号，别名覆盖中文名与其他写法，避免归一后与原单对不上
PRODUCTS: list[tuple[str, str, str, str]] = [
    ("YUJ-18BZ", "YUJ-18BZ", "YUJ-18BZ,YUJ18BZ,鱼机,YU-J18BZ,鱼式制丸机", "鱼机 18BZ"),
]

# 材料字典：name, code, aliases, spec
MATERIALS: list[tuple[str, str, str, str]] = [
    ("Q235", "Q235", "Q235,Q235B,235", "碳素结构钢"),
    ("45#", "45", "45#,45号钢,45钢", "优质碳素结构钢"),
    ("304", "304", "304,0Cr18Ni9,SUS304,不锈钢", "不锈钢"),
    ("HT200", "HT200", "HT200,铸铁,灰铸铁", "灰铸铁"),
]

# code, name, 所属工种, 识别别名
# 工艺工序字典。别名来自 doc/9月2车间工时核算.xlsx 的工序列实际写法
# （车1~车5、铣1~铣4、机架刨、钳装…），由 scripts/import_ledger.py 回填。
PROCESSES: list[tuple[str, str, str, str]] = [
    ("TANG", "镗", "镗工", "镗,镗工,镗床"),
    ("XI", "铣", "铣工", "铣,铣工,铣1,铣2,铣3,铣4,铣床"),
    ("LJIA", "立加", "立加", "立加,立式加工中心,立加1,立加2"),
    ("JGZX", "加工中心", "加工中心", "加工中心,cnc,CNC,加工中芯"),
    ("CHE", "车", "车工", "车,车工,车床,数车,数控车,车1,车2,车3,车4,车5"),
    ("QIAN", "钳", "钳工", "钳,钳工,钳1,钳2,钳装,气缸钳装,退回翻新钳拆"),
    ("MO", "磨", "磨工", "磨,磨工,磨床,磨1,磨2,翻板磨,主轴磨"),
    ("JINGMO", "精磨", "磨工", "精磨"),
    ("XIAN", "线切割", "线切割", "线,线切割,线切"),
    ("ZHEWAN", "折弯", "折弯", "折弯,折,辅助折弯"),
    ("BAO", "刨", "刨工", "刨,刨床,机架刨,导向轴座刨,龙门刨"),
    ("DIANGONG", "电工", "电工", "电工,电气,拆线,拆线打包,火焰切割接线"),
    ("HAN", "焊", "焊工", "焊,焊接,组焊"),
    ("ZHUANGPEI", "装配", "装配", "装配,组装,沾输送带"),
    ("SHIJI", "试机", "装配", "试机,调试,试程序,试自动程序,空载试机"),
    ("PEIPAN", "配盘", "电工", "配盘,配电"),
    ("XIEHUO", "卸车", "辅助", "卸车"),
    ("SONGHUO", "送货", "辅助", "开车送货,送货"),
    ("PENSHA", "喷砂", "辅助", "喷砂,打砂"),
    ("XIALIAO", "下料", "下料", "下料,激光下料,火焰下料"),
    ("HUAXIAN", "划线", "钳工", "划线,划,放样"),
    ("ZUANKONG", "钻孔", "钳工", "钻孔,钻,输送带链条钻孔"),
    ("CHA", "插", "插工", "插,插床,插削"),
    ("JIAOZHI", "校直", "钳工", "校直,矫直"),
    ("DAOJIAO", "倒角", "钳工", "倒角"),
    ("DABAO", "打包", "辅助", "打包,装车,包装,装箱"),
]

DEFAULT_PERIODS = [("2026-09", "2026年9月")]


def seed_all(db: Session) -> dict[str, int]:
    created = {
        "workers": 0,
        "processes": 0,
        "periods": 0,
        "roles": 0,
        "products": 0,
        "materials": 0,
        "links": 0,
    }

    # ---- 角色
    # 内置角色的名字跟标准表的印刷体对齐（调度员→计调员、领料员→材料员），
    # 老库里的名字要跟着改，否则界面上还是旧叫法，跟单子对不上。
    for idx, (code, name, kind, desc) in enumerate(ROLES):
        exists = db.scalar(select(Role).where(Role.code == code))
        if exists:
            if exists.builtin and (exists.name != name or exists.description != desc):
                exists.name = name
                exists.description = desc
            continue
        db.add(
            Role(code=code, name=name, kind=kind, description=desc, builtin=True, sort_order=idx)
        )
        created["roles"] += 1
    db.flush()
    role_map = {r.code: r.id for r in db.scalars(select(Role)).all()}

    # ---- 人员
    for name, main_p, extra in WORKERS:
        exists = db.scalar(select(Worker).where(Worker.name == name))
        if exists:
            continue
        db.add(
            Worker(
                name=name,
                team="2车间",
                default_process=main_p + (f"/{extra}" if extra else ""),
                aliases="",
            )
        )
        created["workers"] += 1

    for name in INSPECTORS:
        exists = db.scalar(select(Worker).where(Worker.name == name))
        if exists:
            if not exists.is_inspector:
                exists.is_inspector = True
            continue
        db.add(Worker(name=name, team="质检", default_process="检验", is_inspector=True))
        created["workers"] += 1
    db.flush()

    # ---- 人员角色绑定（老库补绑：工人 -> 工作者，检验员 -> 检验员）
    for w in db.scalars(select(Worker)).all():
        has = db.scalar(select(WorkerRole).where(WorkerRole.worker_id == w.id))
        if has:
            continue
        codes = ["INSPECTOR"] if w.is_inspector else ["WORKER"]
        for code in codes:
            rid = role_map.get(code)
            if rid:
                db.add(WorkerRole(worker_id=w.id, role_id=rid))
                created["links"] += 1

    # ---- 工序
    seen_codes: set[str] = set()
    for idx, (code, name, category, aliases) in enumerate(PROCESSES):
        if code in seen_codes:
            continue
        seen_codes.add(code)
        exists = db.scalar(select(ProcessDict).where(ProcessDict.code == code))
        if exists:
            continue
        db.add(
            ProcessDict(
                code=code,
                name=name,
                aliases=aliases,
                category=category,
                count_hours=True,
                active=True,
                sort_order=idx,
            )
        )
        created["processes"] += 1

    # ---- 产品
    for idx, (name, code, aliases, spec) in enumerate(PRODUCTS):
        if db.scalar(select(ProductDict).where(ProductDict.name == name)):
            continue
        db.add(
            ProductDict(name=name, code=code, aliases=aliases, spec=spec, active=True, sort_order=idx)
        )
        created["products"] += 1

    # ---- 材料
    for idx, (name, code, aliases, spec) in enumerate(MATERIALS):
        if db.scalar(select(MaterialDict).where(MaterialDict.name == name)):
            continue
        db.add(
            MaterialDict(name=name, code=code, aliases=aliases, spec=spec, active=True, sort_order=idx)
        )
        created["materials"] += 1

    # ---- 核算期
    for ym, title in DEFAULT_PERIODS:
        exists = db.scalar(select(Period).where(Period.ym == ym))
        if exists:
            continue
        db.add(Period(ym=ym, title=title))
        created["periods"] += 1

    db.commit()
    return created
