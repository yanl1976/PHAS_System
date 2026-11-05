"""数据模型：核算期 / 批次 / 图片 / 单据 / 工时明细 / 审计 / 工价。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _now() -> dt.datetime:
    return dt.datetime.now()


class Role(Base):
    """岗位角色。两件事合在一张表里，但含义分开：

    * `kind` 决定该角色的人进入哪一份识别名册（工作者/检验员/接交人…）；
    * `duties` 决定这个岗位**能承担哪几件系统功能**（任务编制/工艺卡编制/报工/工时单价）。

    前者管「单据上叫什么」，后者管「系统里能做什么」。一个人可以挂多个岗位，
    职能取并集——这就是「一人多岗」。内置角色的 kind 固定（历史单据匹配不能漂），
    但 duties 随时可以调，因为它只影响以后的操作，不改历史数据。
    """

    __tablename__ = "role"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(32))
    # 识别名册归类：worker / inspector / receiver / other
    kind: Mapped[str] = mapped_column(String(16), default="other", index=True)
    description: Mapped[str] = mapped_column(String(128), default="")
    # 岗位职能：逗号分隔的职能码（见 domain/sys_roles.py 的 DUTY_CODES）。
    # 空串 = 这个岗位没配职能，账号按系统角色的默认职能走。
    duties: Mapped[str] = mapped_column(Text, default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class Worker(Base):
    """人员名册。手写姓名识别走封闭集匹配，别名用于兜住识别变体。"""

    __tablename__ = "worker"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    employee_no: Mapped[str] = mapped_column(String(32), default="")  # 工号
    aliases: Mapped[str] = mapped_column(Text, default="")  # 逗号分隔的识别变体
    team: Mapped[str] = mapped_column(String(32), default="2车间")
    default_process: Mapped[str] = mapped_column(String(32), default="")  # 工种
    is_inspector: Mapped[bool] = mapped_column(Boolean, default=False)  # 派生字段，随角色同步
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    roles: Mapped[list["WorkerRole"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )


class WorkerRole(Base):
    """人员 ↔ 角色。一个人可兼多个角色（老师傅既干活也带检验）。"""

    __tablename__ = "worker_role"
    __table_args__ = (UniqueConstraint("worker_id", "role_id", name="uq_worker_role"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("worker.id"), index=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("role.id"), index=True)

    worker: Mapped[Worker] = relationship(back_populates="roles")
    role: Mapped[Role] = relationship()


class WorkerRate(Base):
    """工时单价：某人从某个核算期起生效的时薪（元/小时）。

    为什么按「生效月」存一条条记录，而不是在 worker 上放一个当前单价：
    工资核算要能回溯。9 月调过价，8 月的工资表必须还按 8 月的老价算得出来；
    只留「当前值」的话，一改价，历史工资表就跟着变，而工资发出去是不会变的。

    取价规则：`effective_ym <= 核算月` 里最新的那条；没有则视为未设单价（0 元），
    界面上要标出来 —— 未设单价的人的工资是算不出来的，不能静默当 0 发。
    """

    __tablename__ = "worker_rate"
    __table_args__ = (
        UniqueConstraint("worker_id", "effective_ym", name="uq_worker_rate"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("worker.id"), index=True)
    effective_ym: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM
    rate: Mapped[float] = mapped_column(Float, default=0.0)           # 元/小时
    note: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    worker: Mapped[Worker] = relationship()


class ProductDict(Base):
    """产品字典。表头「产品名称/代号」按此收敛，避免同型号多种写法。"""

    __tablename__ = "product_dict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    code: Mapped[str] = mapped_column(String(64), default="")   # 产品代号
    aliases: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[str] = mapped_column(String(64), default="")   # 规格型号
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MaterialDict(Base):
    """材料字典。表头「材料」栏按此收敛。"""

    __tablename__ = "material_dict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    code: Mapped[str] = mapped_column(String(32), default="")
    aliases: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[str] = mapped_column(String(64), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class ProcessDict(Base):
    """工序字典。汇总时按此归一，避免同工序多种写法。"""

    __tablename__ = "process_dict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(32))
    aliases: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="")  # 所属工种
    count_hours: Mapped[bool] = mapped_column(Boolean, default=True)
    # 工艺默认序：同一道工序在工艺路线里通常出现在第几段（下料→车铣→钳→焊→装配）。
    # 「从历史生成路线」时按此排序，比按样本数排更贴近真实工艺。
    # 与 sort_order（列表显示顺序）分开：显示顺序是录入顺序，不是工艺顺序。
    default_seq: Mapped[int] = mapped_column(Integer, default=999)
    active: Mapped[bool] = mapped_column(Boolean, default=True)  # 停用后不参与识别匹配
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class PartDict(Base):
    """件名字典（零部件名称）。

    加工路线单的「件名」栏就是这些零件名（机架 / 刀轮 / 进料斗…），
    和工序是两回事：工序决定怎么干，件名决定干的是什么零件，两者组合才定得出单件工时。
    没有这份名册，手写的件名只能开放识别，错字无法收敛。
    """

    __tablename__ = "part_dict"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    aliases: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(32), default="")  # 所属设备/产品族
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class StandardHour(Base):
    """单件工时标准：(产品, 件名, 工序) → 单件工时。

    从历史工时账统计而来，用途是**校验**而不是替代识别：
    手写「4:10:00」时拿标准值比一下，差得离谱就告警；单据没写单件工时也能兜底。
    键必须带上件名——同一个产品同一道工序，不同零件的工时能差几倍。
    """

    __tablename__ = "standard_hour"
    __table_args__ = (UniqueConstraint("product", "part", "process", name="uq_std_hour"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 产品主数据外键。product 文本是历史写法（SJ2栓剂 / YUJ18BZ制丸机 …），
    # 归属和按产品统管一律走 product_id；文本只用于显示和兼容旧接口。
    product_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    part: Mapped[str] = mapped_column(String(64), default="", index=True)
    process: Mapped[str] = mapped_column(String(32), default="", index=True)
    unit_hours: Mapped[float] = mapped_column(Float, default=0.0)
    samples: Mapped[int] = mapped_column(Integer, default=0)   # 统计样本数
    agreement: Mapped[float] = mapped_column(Float, default=0.0)  # 样本一致率
    # 贡献这条定额的工时明细 id 列表（JSON 数组）。统计是物化的，
    # 但每个数字都要能点回「哪张单、哪个人、哪一天」，否则定额就成了无源之水。
    sample_item_ids: Mapped[str] = mapped_column(Text, default="[]")
    # 数据从哪来：item=由本系统工时明细统计而来（可追到单据）；
    # import=从外部台账（doc/9月2车间工时核算.xlsx）导入，追不到单据。
    # 现状是 1279 条几乎全是 import —— 这本身就是「没有任务来源」的证据。
    origin: Mapped[str] = mapped_column(String(16), default="import")
    computed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class WorkTask(Base):
    """生产任务 = **一个订单**。

    一个任务由多条工艺路线组成（见 `TaskRoute`）：订单里有几种「产品×件名」，
    就有几条路线。路线本身**引用**已有基线，而不是复制一份：

        任务（订单）
          ├─ 路线 A ──→ 基线 YUJ-18BZ/料包   （共用，改基线所有任务同步）
          ├─ 路线 B ──→ 基线 YUJ-18BZ/刀具   （共用）
          └─ 路线 C ──→ 本单副本（fork 而来，只影响这一单）
                        └─ 实报工时（work_item，经 sheet 归属）

    为什么任务不等于单据：一张加工路线单只是某次加工的凭证，一个订单会拆给
    多个班组、多张单、跨多天；单据可以重传作废，订单号是业务锚点，必须独立存在。
    所以 `sheet.task_id` 是多对一，而不是反过来。

    为什么路线是引用而不是快照：1279 条标准工时对应 85 条基线，若每个任务都复制
    一份，一年就复制出几千条几乎相同的路线，「标准」这件事立刻失去意义。
    只有确实要为本单改工艺时才 fork，fork 之后两边互不污染。

    `product` / `product_id` 是**派生字段**（取第一条路线的产品），只为列表识别
    与按产品过滤，权威归属永远在 task_route 上。
    """

    __tablename__ = "work_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 订单号 / 任务号，全局唯一：YUJ-18BZ-26-1-202609
    task_no: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")     # 订单名（手填，便于识别）
    customer: Mapped[str] = mapped_column(String(128), default="")
    product_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)  # 派生
    product: Mapped[str] = mapped_column(String(128), default="", index=True)           # 派生
    period_id: Mapped[int | None] = mapped_column(ForeignKey("period.id"), index=True, nullable=True)
    order_date: Mapped[str] = mapped_column(String(10), default="")  # YYYY-MM-DD
    due_date: Mapped[str] = mapped_column(String(10), default="")
    qty: Mapped[float] = mapped_column(Float, default=0.0)           # 订单数量（台/套）
    actual_hours: Mapped[float] = mapped_column(Float, default=0.0)  # 物化：已确认单据实报合计
    sheet_count: Mapped[int] = mapped_column(Integer, default=0)     # 物化：已确认单据数
    # 待核对不能混进「实报」：汇总只认已确认单据，任务上若把未确认的也算进实报，
    # 同一批单据在汇总页和任务页会给出两个数。两个口径分开存，界面上分别显示。
    pending_hours: Mapped[float] = mapped_column(Float, default=0.0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    # draft（刚建未开）/ open（在制）/ closed（完工结案）/ cancelled（取消）
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual/sheet/plan
    remark: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    # 任务版本号：**基本信息**（数量、交期、期间、客户、任务号…）每改一次 +1。
    # 为什么要版本：任务的基本信息是成本归集与月度汇总的口径。
    # 「这张单当初是按多少台、什么交期下的」如果答不上来，事后对账就没有锚点。
    # 改状态（在制/结案）与备注不算改口径，不升版。
    version: Mapped[int] = mapped_column(Integer, default=1, index=True)
    revised_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    revised_by: Mapped[str] = mapped_column(String(32), default="")


class TaskRevision(Base):
    """任务基本信息的每一次修订 = 一个版本快照（只记变了什么，不存全量）。

    与 `route_version_notice` 同一思路：升版必须留痕，否则「谁在什么时候把
    这单从 100 台改成 120 台」只能靠猜。`changes` 存 [{"field","label","old","new"}]，
    界面直接照着念，不做二次解释。
    """

    __tablename__ = "task_revision"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("work_task.id"), index=True)
    # 升到哪一版。v1 是建单时的初版，没有修订记录即代表从未改过口径
    version: Mapped[int] = mapped_column(Integer, default=1)
    changes: Mapped[str] = mapped_column(Text, default="[]")
    note: Mapped[str] = mapped_column(String(255), default="")
    user: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    __table_args__ = (UniqueConstraint("task_id", "version", name="uq_task_revision_ver"),)


class TaskRoute(Base):
    """任务下的一条工艺路线（**引用**，不是复制）。

    `origin` 三种：

    * `baseline` —— 引用产品×件名的基线路线。多任务共用同一份，改基线立即生效。
      这是默认，也是「不用重复新增工艺路线」的落点。
    * `copy` —— 从基线 fork 出来的副本（`process_route.kind='task'`），只属于本任务，
      之后改它不影响基线，改基线也不影响它。
    * `manual` —— 本任务从零手建的路线，没有基线来源。

    `product/part/batch_no/qty` 是本任务这一条路线的口径：同一个件名在不同订单里
    可能批次不同、数量不同，所以数量记在关联上，不记在路线（路线是标准，不带单量）。
    """

    __tablename__ = "task_route"
    __table_args__ = (
        UniqueConstraint("task_id", "route_id", name="uq_task_route"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("work_task.id"), index=True)
    route_id: Mapped[int] = mapped_column(ForeignKey("process_route.id"), index=True)
    origin: Mapped[str] = mapped_column(String(16), default="baseline", index=True)
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    part: Mapped[str] = mapped_column(String(128), default="", index=True)
    batch_no: Mapped[str] = mapped_column(String(64), default="")
    qty: Mapped[float] = mapped_column(Float, default=0.0)   # 本单该件名数量
    seq: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)  # active/removed
    note: Mapped[str] = mapped_column(String(255), default="")
    # 「这一版标准，本单有人看过并认可了」。发布升版会把引用重指到新版，
    # 但重指是系统的动作，不等于干活的人知道定额变了 —— 已开工的活按哪版做，
    # 必须有人点过头。seen_route_id 落后于 route_id 就是「待确认升版」，
    # 所有引用处要红字提醒，保存/提交单据时也要再提醒一次。
    seen_route_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    seen_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class ProcessRoute(Base):
    """工艺路线：某个「产品×件名」的有序工序清单。

    一条记录 = 一版路线。`kind` 三种：

    * `route` —— 基线工艺（主数据），界面上叫「基线」。产品×件名的标准工序，
      是定额的**主口径**。可以没有任务（新品还没投产），但 `product_id` 必填。
      取值沿用 `route` 而不另立 `baseline`：全库查询、前端参数、8 组测试
      都按 `kind='route'` 走，改名只会换来一堆兼容分支，语义在这里讲清楚即可。
    * `task` —— 本单副本（fork）。只有确实要为一个任务改动工艺时才生成，
      从基线复制一份，此后独立演进：改基线不影响已开工任务，改任务也不污染基线。
      `task_id` 必填。默认不加副本——路线通过 `task_route` 引用基线即可复用。
    * `template` —— 可复用的路线模板（P1 启用，套用到具体件名时生成 route）。

    关键设计：**允许片段**。`completeness='partial'` 表示只登记了已知工序，
    不代表该零件的全部工艺。因此校验环节绝不报「缺工序」——那样会天天误报。

    「任务来源」为什么必须单独建表：一条路线如果只有 `source='history'` 这种类型标记，
    就永远答不出「这条定额是哪张单、哪几个人、哪一天算出来的」，产品维度上也就
    统管不了「任务 → 工艺 → 实报」这条链。所以 product_id / task_id 是归属，
    snapshot_of_id 是来源，缺一不可。

    与 `standard_hour`（历史实测统计，只读）的关系：
    实测值是路线的**输入**，路线的定额是**主口径**。两者在界面对照显示偏差，
    但绝不互相覆盖——定额是有人确认过的标准，实测是机器统计的结果。
    """

    __tablename__ = "process_route"
    # 唯一性分两条腿走：基线与任务工艺的取值范围完全不同。
    # 原来是一条 (kind, product, part, version) 的 UNIQUE 约束，任务工艺进来就撞墙了
    # ——同一产品件名的多个任务都是 kind='task'/version=1，会被判成重复。
    # 改成两个部分唯一索引，各自只约束自己那一类。
    __table_args__ = (
        Index(
            "uq_route_baseline",
            "product", "part", "version",
            unique=True,
            sqlite_where=text("kind <> 'task'"),
        ),
        Index(
            "uq_route_task",
            "task_id", "version",
            unique=True,
            sqlite_where=text("kind = 'task'"),
        ),
        # 同一「产品×件名」同时只能有一条已发布基线 —— 这是「只能引用已发布工艺」
        # 能成立的前提。以前只靠 publish() 里把其它版归档来维持，绕开接口
        # 直接改库就能造出两条并行基准，核对时按哪条都说不清。
        Index(
            "uq_route_released",
            "product", "part",
            unique=True,
            sqlite_where=text("kind <> 'task' AND status = 'released'"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # route=基线 / task=任务工艺 / template=模板
    kind: Mapped[str] = mapped_column(String(16), default="route", index=True)
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 产品主数据外键（product_dict.id）。product 文本保留做显示与兼容，
    # 但归属判断一律走 product_id——文本写法在历史数据里有 6 种同型号变体。
    product_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    part: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 任务工艺专用：属于哪个任务。基线/模板必须为空。
    task_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    # 快照来源：这条 task 路线是从哪一版基线复制出来的
    snapshot_of_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    snapshot_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 快照时基线是什么状态（released/draft）。现状是 85 条基线全是草稿，
    # 从草稿快照出来的任务工艺必须能自证「来源未发布」，否则会被当成正式标准用。
    snapshot_status: Mapped[str] = mapped_column(String(16), default="")
    title: Mapped[str] = mapped_column(String(64), default="")   # 模板名 / 路线别名
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft/released/archived
    completeness: Mapped[str] = mapped_column(String(16), default="partial")  # full/partial
    batch_qty: Mapped[float] = mapped_column(Float, default=0.0)   # 标准批量（0 = 不分摊准备工时）
    allowance_rate: Mapped[float] = mapped_column(Float, default=0.0)  # 宽放率
    source: Mapped[str] = mapped_column(String(16), default="manual")  # manual/history/template/sheet/promote
    # 发布留痕：谁、什么时候把这一版定为基准。只记 created_by 回答不了
    # 「现在生效的这版是谁批的」，而基准一旦发布就成了全厂核对的依据。
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    published_by: Mapped[str] = mapped_column(String(32), default="")
    effective_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    effective_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    remark: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    steps: Mapped[list["ProcessRouteStep"]] = relationship(
        back_populates="route", cascade="all, delete-orphan", order_by="ProcessRouteStep.seq"
    )


class RouteVersionNotice(Base):
    """升版通知：某条基线由 vX 换成 vY 这件事，以及它影响了多少在制任务。

    为什么要单独一张表，而不是让前端比版本号：

    * 版本号只说「有新版本」，说不出「谁在什么时候因为什么改的」。现场要认的是
      后者 —— 定额从 1.2h 变成 1.5h，是工艺科改的、哪天生效的，得能看见。
    * 引用可能已经被重指（走的是引用语义），光比 route_id 与 latest 是比不出来的：
      任务早已指向新版，但并没有人确认过这个变化。
    * 通知要能被"逐条消化"：任务侧记住自己确认到哪一版，跨页面提醒才能收敛。

    `from_route_id` 允许为空（新品首次发布没有「前一版」），此时不算「升版」，
    不产生待确认项。只有真的替换了在用的基准，才需要人确认。
    """

    __tablename__ = "route_version_notice"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[int] = mapped_column(Integer, index=True)       # 新版本
    from_route_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 被替换的旧版
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    part: Mapped[str] = mapped_column(String(128), default="", index=True)
    from_version: Mapped[int] = mapped_column(Integer, default=0)
    to_version: Mapped[int] = mapped_column(Integer, default=0)
    # publish=编排页发布 / edit=任务抽屉内改基线 / promote=副本进阶 / batch=批量发布
    action: Mapped[str] = mapped_column(String(16), default="publish")
    actor: Mapped[str] = mapped_column(String(32), default="")
    scope_tasks: Mapped[int] = mapped_column(Integer, default=0)  # 受影响的在制任务数
    unit_delta: Mapped[float] = mapped_column(Float, default=0.0)  # 单件工时变化（新 - 旧）
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, index=True)


class WorkCombo(Base):
    """工时组合（产品 × 件名）—— 工艺的**身份**，路线只是它的某一版。

    为什么必须单独建这张表：

    在这之前，「889 个组合」是 `standard_hour ∪ process_route` 每次现算出来的
    并集，没有实体。于是三件事都答不出来：这个组合归不归工艺科管、它的工艺
    到底确不确认过、它装在哪个部件上。所谓「状态」也只是从路线状态反推的
    —— 迁移脚本批量把 85 条路线标成 released 的那一刻，台账上就凭空出现了
    「81 个已发布组合」，没有人签过字。

    所以把身份独立出来，三张表分工定死：

        WorkCombo    身份 + 对外可用状态 + 部件层级
          ├→ ProcessRoute     版本（draft / released / archived，已有机制）
          │     └→ ProcessRouteStep   工序与定额
          └→ StandardHour     历史实测，只做参考与建线输入

    `status` 是**对外**语义：只有 released 的组合才会出现在任务挂线、识别校验、
    工艺卡里。草稿意味着「工艺科还没认」，它连被引用的资格都没有 —— 这比
    「有路线就算数」严格，也是本表存在的理由。

    `route_id` 记「当前生效的那一版」。路线侧本来就有 released 唯一索引兜底，
    这里再记一次是为了让「组合 → 生效工艺」是一次直读，不必每次回表比对版本号。
    """

    __tablename__ = "work_combo"
    __table_args__ = (
        UniqueConstraint("product", "part", name="uq_combo_pair"),
        Index("ix_combo_status", "status"),
        Index("ix_combo_parent", "parent_id"),
        Index("ix_combo_prod", "product"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    part: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 产品主数据外键。文本仍保留：历史写法有 6 种同型号变体，显示用文本、
    # 归属与「按产品分类」一律走 product_id，否则分类树会裂成好几棵。
    product_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)

    # ---- 部件层级：产品（总装）→ 部件 → 子件 ----
    # level=0 是根（整机装配/总装），level=1 是直接挂在根下的部件或零件，
    # level>=2 是部件下的子件。不设上限，但校验限制到 5 层，再深人就看不清了。
    level: Mapped[int] = mapped_column(Integer, default=1)
    # 上级组合 id（同产品内）。根为 NULL。用自引用而不是另建 BOM 表：
    # 一条组合只装在一个上级上，多上级属于 BOM 用量问题，那要专门的 BOM 表，
    # 不该让工艺组合表兼职。
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qty_per_unit: Mapped[float] = mapped_column(Float, default=1.0)  # 装一台上级用几件
    seq: Mapped[int] = mapped_column(Integer, default=100)           # 同级排序
    # 层级是谁定的：auto=按件名规则自动铺（迁移时），manual=工艺员改过。
    # 分开记是因为「自动铺错了」和「人工认定」要区别对待 —— 前者可以重铺，
    # 后者重铺会把人的判断冲掉。
    level_src: Mapped[str] = mapped_column(String(16), default="auto")

    # ---- 对外状态：draft 一律不可引用 ----
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    route_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    published_by: Mapped[str] = mapped_column(String(32), default="")
    # 降级留痕：从哪个状态降下来的（目前只有 released）。迁移把批量标出来的
    # 「假发布」降草稿时记 'released'，台账据此提示「这条曾经被标过已发布」。
    demoted_from: Mapped[str] = mapped_column(String(16), default="")
    note: Mapped[str] = mapped_column(String(255), default="")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ProductNode(Base):
    """产品结构（装配关系）—— 独立于工艺的装配事实。

    为什么从 `work_combo` 里抽出来：结构和工艺是两件事。

    * 结构回答「这台机器由哪些件组成、谁装在谁下面、每台几件、同级先后」，
      由设计/技术决定。一个件哪怕一道工序都没编，它在结构里照样存在、照样有件号。
    * 工艺回答「这个件怎么做、几道工序、多少工时」，挂在「件」上（产品 × 件名），
      跟它装在谁下面无关。

    之前层级字段寄居在 `work_combo` 上，代价是三个真实问题：

    1. 没有工时组合的件在结构里根本不存在 —— 结构天生不完整；
    2. 一个件只能挂在一个上级下（自引用 `parent_id`），真实 BOM 里同一个件
       常装在多个部件上；
    3. 删/改名工时组合会连带把结构改坏。

    现在分成两张表：本表只存结构，`work_combo` 只存工艺身份与发布状态，
    两边以 `(product, part)` 关联。工艺卡和工时汇总沿本表展开（件号、用量加权）。

    `WORK_COMBO` 上那几个层级列保留不删，但**已停止读写**（老库迁移要读一次），
    避免历史上直接查它们的脚本突然 500。
    """

    __tablename__ = "product_node"
    __table_args__ = (
        UniqueConstraint("product", "part", name="uq_node_pair"),
        Index("ix_node_product", "product"),
        Index("ix_node_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product: Mapped[str] = mapped_column(String(128), default="", index=True)
    part: Mapped[str] = mapped_column(String(128), default="", index=True)
    # 上级节点（同产品内）。根为 NULL。
    parent_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[int] = mapped_column(Integer, default=1)
    qty_per_unit: Mapped[float] = mapped_column(Float, default=1.0)  # 装一台上级用几件
    seq: Mapped[int] = mapped_column(Integer, default=100)           # 同级排序

    # ---- 计划生产数量（排产输入，不是设计定额）----
    # 和 `qty_per_unit` 是两件事，别混：
    #   qty_per_unit = 装一台上级用几件（设计定额，不随订单变）
    #   plan_qty     = 这一批做几件（排产输入，跟着批次走）
    # 子件默认由「父件计划量 × 本级用量」派生（plan_src='auto'）；
    # 人工改过就标 'manual' 并保留 —— 否则每次动上级都会把人的决定冲掉。
    # 反向改动（改子件）会按用量把上级反推上去，见 services/node_plan.py。
    plan_qty: Mapped[float] = mapped_column(Float, default=1.0)
    plan_src: Mapped[str] = mapped_column(String(16), default="auto")
    # auto=按件名规则自动铺（迁移/重铺），manual=人改过。重铺时只动 auto。
    source: Mapped[str] = mapped_column(String(16), default="auto")
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ProcessRouteStep(Base):
    """工艺路线的一步（工序行）。"""
    __tablename__ = "process_route_step"
    __table_args__ = (UniqueConstraint("route_id", "seq", name="uq_route_step_seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # ondelete CASCADE：删路线时数据库层自动带走工序行。
    # 关系上的 cascade="all, delete-orphan" 只在 ORM 层生效，
    # db.query(...).delete() 这类批量删除绕过它 → 必须靠数据库兜底，
    # 否则删父行会撞 FOREIGN KEY constraint failed。
    route_id: Mapped[int] = mapped_column(
        ForeignKey("process_route.id", ondelete="CASCADE"), index=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=10)          # 工序号，10/20/30 便于插行
    process_code: Mapped[str] = mapped_column(String(32), default="")
    process_name: Mapped[str] = mapped_column(String(32), default="")
    op_type: Mapped[str] = mapped_column(String(16), default="self")  # self/outsource/inspect
    category: Mapped[str] = mapped_column(String(32), default="")   # 工种
    work_center: Mapped[str] = mapped_column(String(64), default="")  # 设备/工位
    setup_hours: Mapped[float] = mapped_column(Float, default=0.0)  # 准备工时（每批）
    unit_hours: Mapped[float] = mapped_column(Float, default=0.0)   # 单件工时定额
    hours_src: Mapped[str] = mapped_column(String(16), default="manual")  # history/manual/sheet
    samples: Mapped[int] = mapped_column(Integer, default=0)  # 生成时的历史样本数
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)      # 关键工序
    inspect_required: Mapped[bool] = mapped_column(Boolean, default=False)
    sop: Mapped[str] = mapped_column(String(255), default="")  # 作业要求 / 图纸号
    remark: Mapped[str] = mapped_column(String(255), default="")

    route: Mapped[ProcessRoute] = relationship(back_populates="steps")


class Period(Base):
    """核算期（月度）。锁定后明细只读。"""

    __tablename__ = "period"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ym: Mapped[str] = mapped_column(String(7), unique=True, index=True)  # 2026-09
    title: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / locked
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    locked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)

    batches: Mapped[list["Batch"]] = relationship(back_populates="period")


class Batch(Base):
    """一次导入批次，通常对应一叠纸的批量拍照。"""

    __tablename__ = "batch"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id"), index=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    source: Mapped[str] = mapped_column(String(64), default="photo")
    created_by: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    period: Mapped[Period] = relationship(back_populates="batches")
    images: Mapped[list["Image"]] = relationship(back_populates="batch")


class Image(Base):
    """原始图片与处理状态。"""

    __tablename__ = "image"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("batch.id"), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    path: Mapped[str] = mapped_column(String(512))
    processed_path: Mapped[str] = mapped_column(String(512), default="")
    thumb_path: Mapped[str] = mapped_column(String(512), default="")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    skew_angle: Mapped[float] = mapped_column(Float, default=0.0)
    red_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/processing/done/failed
    error: Mapped[str] = mapped_column(Text, default="")
    # 识别队列外置（C1）：Web 多进程后，队列不能再放在某个进程的内存里。
    # `image.status` 就是队列本身，下面三个字段让它能被安全领取与限次重试。
    # attempts：被领取过几次。毒图（超大图/引擎段错误）会反复失败，
    #           没有上限就会「重启→崩溃→重启」永远起不来。
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # 重新识别要保留的人工修改（表头/各明细行）。以前靠函数参数传给线程，
    # 现在识别在另一个进程里跑，参数必须落库才能带过去。
    keeps_json: Mapped[str] = mapped_column(Text, default="")
    uploaded_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)

    batch: Mapped[Batch] = relationship(back_populates="images")
    sheet: Mapped["Sheet | None"] = relationship(back_populates="image", uselist=False)


class PrintSheet(Base):
    """系统**预印**的一张《加工路线单》—— 纸质凭证在系统里的镜像。

    与 `Sheet` 的区别（很重要，别混）：

    * `Sheet` 是**别人手写完、我们拍照识别**进来的那张纸，带有照片、OCR 原文、
      版面还原结果，`image_id` 必填。
    * `PrintSheet` 是**我们自己印出去、等着人签字**的那张纸。它没有照片，
      只有一张「印的时候是什么样」的快照；因此工艺后来改了，已印出去的纸
      也不能跟着变（`PrintSheetLine` 就是这份快照）。

    `form_no` 是印刷体单号，也是**幂等键**：同一张纸可能既被 OCR 识别到、
    又被计划员扫码报工，两条通道必须以这个号互认，否则同一批工时进两次账，
    工资直接发错。识别侧命中已有单号时只告警，不重复入账。
    """

    __tablename__ = "print_sheet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # RG2609-00001
    task_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    product_name: Mapped[str] = mapped_column(String(128), default="")
    batch_no: Mapped[str] = mapped_column(String(64), default="")
    part_name: Mapped[str] = mapped_column(String(128), default="")   # 件名
    part_no: Mapped[str] = mapped_column(String(32), default="")      # 件号 01-01
    route_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    route_version: Mapped[int] = mapped_column(Integer, default=1)
    qty_plan: Mapped[float] = mapped_column(Float, default=0.0)       # 投产台数
    part_qty: Mapped[float] = mapped_column(Float, default=0.0)       # 加工件数
    total_hours: Mapped[float] = mapped_column(Float, default=0.0)    # 印在纸上的定额合计
    # 扫码验签用的一次性令牌。存在库里（而不是纯算法签名）是为了能「作废」：
    # 单子印错、纸丢了、零件报废不做了，作废后那张纸再扫就进不来。
    token: Mapped[str] = mapped_column(String(16), default="", index=True)
    status: Mapped[str] = mapped_column(String(16), default="printed", index=True)
    # printed（已打印待报工）/ reported（已报工）/ void（已作废）
    printed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    printed_by: Mapped[str] = mapped_column(String(32), default="")
    reported_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    reported_by: Mapped[str] = mapped_column(String(32), default="")
    note: Mapped[str] = mapped_column(Text, default="")

    lines: Mapped[list["PrintSheetLine"]] = relationship(
        back_populates="form", cascade="all, delete-orphan", order_by="PrintSheetLine.row_no"
    )


class PrintSheetLine(Base):
    """预印单上的一行 = 一道工序，**打印那一刻的快照**。

    为什么要快照、而不是每次渲染时去查当前工艺：纸已经发到工位了，
    工艺员后来又改了定额，纸上的数和库里的数就会对不上 —— 报工时按
    「纸上那个数」还是「库里新数」算工资，都会有人不服。所以印出去的那一刻
    就把工序、定额、件数全部固化，之后只认这份快照。
    """

    __tablename__ = "print_sheet_line"
    __table_args__ = (UniqueConstraint("form_id", "row_no", name="uq_print_line_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    form_id: Mapped[int] = mapped_column(
        ForeignKey("print_sheet.id", ondelete="CASCADE"), index=True
    )
    row_no: Mapped[int] = mapped_column(Integer, default=1)  # 版面上的第几行 1..10
    seq: Mapped[int] = mapped_column(Integer, default=10)    # 工序号
    process_code: Mapped[str] = mapped_column(String(32), default="")
    process_name: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(32), default="")
    work_center: Mapped[str] = mapped_column(String(64), default="")
    unit_hours: Mapped[float] = mapped_column(Float, default=0.0)
    qty_plan: Mapped[float] = mapped_column(Float, default=0.0)      # 该行计划件数
    total_hours: Mapped[float] = mapped_column(Float, default=0.0)   # 单件 × 件数
    worker_name: Mapped[str] = mapped_column(String(32), default="")  # 预派工（可空）
    is_key: Mapped[bool] = mapped_column(Boolean, default=False)

    form: Mapped[PrintSheet] = relationship(back_populates="lines")


class Sheet(Base):
    """一张加工路线单 = 一张单据。"""

    __tablename__ = "sheet"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("image.id"), unique=True, index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("period.id"), index=True)
    # 单据归属的生产任务（一个订单可有多张加工路线单）
    task_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    form_type: Mapped[str] = mapped_column(String(32), default="加工路线单")
    status: Mapped[str] = mapped_column(String(16), default="recognized", index=True)
    # recognized / verified / rejected
    # 这张纸如果是系统预印的，这里是它的印刷体单号（`print_sheet.form_no`）。
    # 识别时用「预印单号」反查：命中了就说明这张纸是我们印出去的，
    # 可以拿预印快照逐格核对（件名/工序/定额有没有被人改过）；
    # 更重要的是拦住「同一张纸既被扫码报工、又被识别入账」的重复计工时。
    print_form_no: Mapped[str] = mapped_column(String(32), default="", index=True)
    header_json: Mapped[str] = mapped_column(Text, default="{}")
    ocr_raw_json: Mapped[str] = mapped_column(Text, default="[]")
    layout_json: Mapped[str] = mapped_column(Text, default="{}")
    page_w: Mapped[int] = mapped_column(Integer, default=0)
    page_h: Mapped[int] = mapped_column(Integer, default=0)
    total_hours: Mapped[float] = mapped_column(Float, default=0.0)
    flag_count: Mapped[int] = mapped_column(Integer, default=0)
    verify_user: Mapped[str] = mapped_column(String(32), default="")
    verify_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    image: Mapped[Image] = relationship(back_populates="sheet")
    items: Mapped[list["WorkItem"]] = relationship(
        back_populates="sheet", cascade="all, delete-orphan", order_by="WorkItem.seq"
    )


class WorkItem(Base):
    """明细行：一个 人 x 件名 x 工序 的工时记录。

    **它是「报工明细」的唯一载体，两种来源共用一张表**：

    * `source='ocr'` —— 从纸质《加工路线单》识别进来（`sheet_id` 必填）
    * `source='scan'` —— 计划员扫预印单上的二维码报工（`print_form_no` 必填）
    * `source='manual'` —— 页面上手工补录

    为什么不给扫码报工另开一张表：工时汇总、工资核算、勾稽核对全都按
    `work_item` 走，另开一张表意味着每条链路都要 UNION 一次，漏一处就是
    「算漏一批人的工资」。宁可让这张表的 `sheet_id` 可空，也不让数据分成两处。
    """

    __tablename__ = "work_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 扫码报工没有照片、没有单据，所以可空。靠 print_form_no 追溯纸质凭证。
    sheet_id: Mapped[int | None] = mapped_column(
        ForeignKey("sheet.id"), index=True, nullable=True
    )
    seq: Mapped[int] = mapped_column(Integer, default=1)

    product_name: Mapped[str] = mapped_column(String(128), default="")  # 产品名称/代号
    batch_no: Mapped[str] = mapped_column(String(64), default="")        # 批次
    part_name: Mapped[str] = mapped_column(String(128), default="")      # 件名
    material: Mapped[str] = mapped_column(String(64), default="")        # 材料
    process: Mapped[str] = mapped_column(String(32), default="")         # 工序（显示值，字典有的写法照单据写）
    process_raw: Mapped[str] = mapped_column(String(64), default="")     # 单据原文（复合工序归并前）
    # 字典标准名。显示值可能是「包板折弯」（字典登记的别名），汇总和标准工时
    # 必须按标准名「折弯」走，否则同一道工序会被拆成好几组
    process_std: Mapped[str] = mapped_column(String(32), default="")
    process_code: Mapped[str] = mapped_column(String(32), default="")
    qualified_qty: Mapped[float | None] = mapped_column(Float, nullable=True)  # 合格件数
    rework_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    scrap_qty: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_hours: Mapped[float | None] = mapped_column(Float, nullable=True)     # 单件工时
    declared_total: Mapped[float | None] = mapped_column(Float, nullable=True)  # 原单手写合计（仅用于比对）
    total_hours: Mapped[float] = mapped_column(Float, default=0.0)             # 合计工时（系统重算）
    finish_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    # 日期格的 OCR 原文（保留标点：「9.10」不能被洗成「910」）。
    # 手写日期里最细的那一笔（9.10 的「1」只有 2px 高）识别网络一律看不见，
    # 整页只能读出「9.0」；留着原文，核对页才能明说「识别到 9.0，请手工填写」，
    # 而不是让人对着一格空日期猜模型到底读到了什么。
    finish_date_raw: Mapped[str] = mapped_column(String(32), default="")
    inspector: Mapped[str] = mapped_column(String(32), default="")
    # 检验员签名格的 OCR 原文。草书签名几乎认不准（「杜文杰」会读成「定生」），
    # 原文必须单独留着：人工改正后要拿它去名册里建别名，下次同样的写法才能自动认出来
    inspector_raw: Mapped[str] = mapped_column(String(32), default="")
    handover: Mapped[str] = mapped_column(String(32), default="")              # 接交人
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("worker.id"), nullable=True)
    worker_raw: Mapped[str] = mapped_column(String(32), default="")            # OCR 原始文本
    conf_json: Mapped[str] = mapped_column(Text, default="{}")
    flags_json: Mapped[str] = mapped_column(Text, default="[]")
    bbox_json: Mapped[str] = mapped_column(Text, default="{}")
    row_state: Mapped[str] = mapped_column(String(16), default="auto")  # auto/edited/manual
    remark: Mapped[str] = mapped_column(String(255), default="")

    # ------------------------------------------------------------ 报工来源（P4）
    # ocr：识别纸质单；scan：扫预印单二维码；manual：页面补录。
    # 三种来源必须能分开统计：识别通道有复核成本，扫码通道没有，
    # 混在一起就永远说不清「这套系统到底省了多少人工」。
    source: Mapped[str] = mapped_column(String(8), default="ocr", index=True)
    # 扫码报工没有 sheet，核算期与任务就必须自己带在身上，否则它既进不了
    # 月度工资（工资按 sheet→period 归属），也归不到任何一个生产任务上。
    # 识别通道这两列为空，走 sheet 上那份（见 aggregation._load_items）。
    period_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    task_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    # 预印单号（印刷体）。它是**幂等键**：同一张纸既可能被 OCR 认到、
    # 也可能被计划员扫到，靠这个号才能拦住重复计工时（重复就是多发工资）。
    print_form_no: Mapped[str] = mapped_column(String(32), default="", index=True)
    # 完成数量：纸面上「数量」栏基本不填，以前只能由 合格+返修+废品 反推。
    # 扫码报工时它是计划员直接输入的第一个数，缺了它就算不出报废率。
    qty_done: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 报工时刻（区别于 finish_date：那是纸面上写的完工日期，可以补填，
    # 也可能被写错；这个是数据真正进系统的时刻，追溯时只信它）
    reported_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    reported_by: Mapped[str] = mapped_column(String(32), default="")
    # 实报工时（可选）：这道活实际干了多久。不参与计件工资，
    # 只用来和定额对照 —— 没有它，定额永远是拍脑袋的数字，没法校准。
    hours_actual: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 计划员确认「纸上操作者签字已核对」。纸质凭证与电子记录的那颗铆钉。
    sign_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    # 数量被调整过（不合格/报废）时必填原因，否则报废数据没法做质量分析
    adjust_reason: Mapped[str] = mapped_column(String(64), default="")

    sheet: Mapped[Sheet] = relationship(back_populates="items")
    worker: Mapped[Worker | None] = relationship()


class AuditLog(Base):
    """字段级修改留痕。"""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), index=True)
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    # 所属单据。明细行在「重新识别」时会被整批删掉重建（id 会变），
    # 只按 entity_id 关联会让历史留痕查不出来，所以额外记一份单据 id。
    sheet_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    # 明细行号。重新识别会把明细行删掉重建（id 全变），只有行号稳定，
    # 靠它才能在下一轮识别时把「人改过哪个字段」重新对到新行上。
    seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field: Mapped[str] = mapped_column(String(64), default="")
    old_value: Mapped[str] = mapped_column(Text, default="")
    new_value: Mapped[str] = mapped_column(Text, default="")
    user: Mapped[str] = mapped_column(String(32), default="system")
    ts: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)


class Rate(Base):
    """工价（可选启用）。工时 x 单价 = 金额。"""

    __tablename__ = "rate"
    __table_args__ = (UniqueConstraint("period_ym", "worker_id", "process", name="uq_rate"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_ym: Mapped[str] = mapped_column(String(7), index=True)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("worker.id"), nullable=True)
    process: Mapped[str] = mapped_column(String(32), default="")
    price_per_hour: Mapped[float] = mapped_column(Float, default=0.0)


class AppUser(Base):
    """登录账号。与 Worker 分开：干活的人不一定用系统，用系统的人不一定干活。

    存的是 PBKDF2 摘要，不是明文；`token_epoch` 在改密/停用后 +1，
    让已经发出去的旧令牌立刻失效（否则停用一个账号还得等 12 小时才生效）。

    ## `role` 与 `worker_id` 是两件事，别合并

    - `role` 是**系统角色**（权限）：admin / editor / viewer，决定登录后能操作什么，
      定义在 `app/domain/sys_roles.py`。
    - `worker_id` 指向**人员名册**里那个人，他的岗位角色（操作员/检验员/接交人）
      在 `worker_role` 那一边，决定工时算谁、识别时名字进哪一列。

    两者只做并列展示，不互相推导：纯管理账号可以没有 `worker_id`（不对应车间任何人），
    而车间工人大多数没有账号。用一个字段兼职两件事，最后一定会在工资表上算错人。

    这里用普通 INTEGER 而不是外键：SQLite 的 ADD COLUMN 不支持带 REFERENCES 的列，
    存量库补出来会跟新建库行为不一致。归属由服务层校验（见 auth.py 的 `_check_worker`）。
    """

    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(32), default="")
    password_hash: Mapped[str] = mapped_column(String(256), default="")
    # 系统角色（权限）：admin / editor / viewer —— 与岗位角色无关
    role: Mapped[str] = mapped_column(String(16), default="editor")
    # 对应的名册人员（可空）：只用于「这个账号是谁」，不参与权限判断
    worker_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    token_epoch: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=_now)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


def _audit_operator_listener(mapper, _connection, target):  # pragma: no cover - 事件钩子
    """审计落库前统一改写成「当前登录人」。

    留痕的价值全在「谁做的」。只要有一个入口还信前端传的 user，这条链就断了；
    与其在每个 handler 里记得改，不如在写库这一刻统一兜住。
    """
    from .security import operator_source, current_operator

    # 系统自己干的事（迁移、识别流水线、脚本）没有会话，保留调用方写明的身份
    if operator_source():
        name = current_operator("")
        if name:
            target.user = name


event.listen(AuditLog, "before_insert", _audit_operator_listener)
