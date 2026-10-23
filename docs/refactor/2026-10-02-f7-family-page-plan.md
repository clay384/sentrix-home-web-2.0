# F7 实施计划：家庭图谱前端（人物与关系）

> 前置：S7a / S7b1 / S7b2 已交付，后端 `family-graph` 六条路由已上线。
> 本段只做 `apps/web/`，**不动后端**。

---

## 0. 为什么不能照抄 1.0：一次真实数据实测

2026-10-02 在 153 上对 `data/sentrix.db`（53 张真实照片、17 条事件、9 个实体）跑了一趟
真实推断（`POST /api/v1/family-graph/runs`）：

```
status=completed  stats={"people":8,"memberships":8,"relationships":0,"portraits":8}
```

`GET /api/v1/family-graph?scope_id=album1` 的真实返回：

```
8 个人，membership 全部 = "unknown"，confidence 全部 = 0.0，relationships = 0 条
```

复现模型调用（`/tmp/repro_family.py`）确认：**模型原始回复就是
`{"memberships":[…全部 unknown, confidence 0.0…],"relationships":[]}`** ——
不是移植代码丢的。原因在数据本身：53 张照片的描述全是
「一群人在山顶上合影」「三个人站在室内」，**没有一处亲属称谓**，而提示词明写
「低频或证据不足必须为 unknown」。模型的行为是对的。

### 这对前端意味着什么

1.0 的图谱**只收 `membership ∈ {family, friend}` 的人**（`people_graph.js:57-64`），
其余全塞进另一个 tab 的纯文字列表。按这个规则，**在上面这份真实数据上，1.0 的图谱是一张空图**——
恰好在你最需要它的时候。

所以 F7 的第一条约束不是「画得更好看」，是：

> **待归类的人必须在图上可见，且必须能就地归类。**

第二条：实测下（以及任何刚导入的相册）图谱是 8 个孤点。
把孤点图当默认视图是坏设计，**默认视图必须是名录**。

---

## 1. 裁决

| # | 问题 | 裁决 | 理由 |
|---|---|---|---|
| ① | 引入图可视化依赖（d3 / cytoscape / react-flow）？ | **不引入** | 家庭图是**分层**结构不是力导向结构（Ancestry / MyHeritage 都用分层）；2.0 前端只有 3 个生产依赖，`2026-09-25-frontend-page-design-and-plan.md:318-326` 明令不引入 UI 库；n 在个位到几十，分层布局是 O(n) 的几十行纯函数。**1.0 的「分辈分」直觉是对的，错在执行**：行内等分 `width*(col+1)/(n+1)` 无碰撞规避，人一多就挤成一条线。 |
| ② | 默认视图 | **名录（头像网格）** | 见 §0。图谱是可切换的第二视图。 |
| ③ | 人物详情：新路由 `/people/:id` 还是查询参数 `?person=` | **查询参数** | `app/router.test.tsx:51` 钉死「路由数 == 导航项数 == 页面文件数」，加一条参数路由要改那条门禁。查询参数同样能刷新保持、能后退、能分享，代价为零。 |
| ④ | 现有「人脸簇审阅」（`PeopleView`）放哪 | **收成一个 tab** | 1.0 这一页本来就是 tab 结构（关系图谱 / 待整理人物）。三个 tab：名录 / 图谱 / 待确认簇。 |
| ⑤ | 写操作范围 | **只做 1.0 有的那几条** | 改名、设成员身份、编辑画像、建/改/删关系、合并相册范围。后端六条路由已经齐了，不新增。 |
| ⑥ | 样式 | **新增 `styles/family.css`，用 `tokens.css` 的变量** | 沿用既有约定（全局类名 + CSS 变量），不引入 Tailwind / CSS Modules。 |

---

## 2. 后端接口（已上线，实测过）

`GET /api/v1/family-graph?scope_id=` 的真实形状：

```
{ scope_id, scope_ids[], run|null,
  people[ { id, scope_id, display_name, membership|null, portrait|null,
            avatar_face_instance_id, appearance_count, representative_media } ],
  relationships[ { subject_entity_id, predicate, object_entity_id,
                   inverse_predicate, confidence, source, state, locked, … } ] }
```

`membership` 与 `portrait` 都是**行对象或 null**（未推断过时是 null），
不是标量——前端要按对象取 `.membership` / `.portrait_text`。

其余五条：

| 方法 | 路径 | 体 |
|---|---|---|
| PATCH | `/family-graph/people/{id}/membership` | `{scope_id, membership}` |
| PATCH | `/family-graph/people/{id}/portrait` | `{scope_id, portrait_text}` |
| PUT | `/family-graph/relationships` | `{scope_id, subject_entity_id, predicate, object_entity_id, inverse_predicate, confidence}` |
| DELETE | `/family-graph/relationships` | 同上（体传参） |
| POST | `/family-graph/scopes/merge` | `{scope_ids}` |
| POST | `/family-graph/runs` | `{scope_id}` |

**头像**：`person.avatar_face_instance_id` 非空时用 `/api/v1/face-instances/{id}/crop`
（与 `PeopleView` 现有的取图方式一致），为空时退回首字圆形。

---

## 3. 文件清单

```
apps/web/src/
├── shared/api/client.ts                    改：+7 个 family 函数 + 类型
├── main.tsx                                改：+import "./styles/family.css"
├── features/people/
│   ├── PeoplePage.tsx                      改：空状态 → tab 外壳
│   ├── family/
│   │   ├── FamilyRoster.tsx                新：名录（搜索 + 筛选 + 网格）
│   │   ├── PersonCard.tsx                  新：单张人物卡
│   │   ├── PersonAvatar.tsx                新：头像（真图 / 首字兜底）
│   │   ├── FamilyGraphView.tsx             新：图谱 SVG + 平移缩放 + 拖拽建关系
│   │   ├── graphLayout.ts                  新：分层布局（纯函数）
│   │   ├── graphLayout.test.ts             新：布局单测
│   │   ├── PersonDetail.tsx                新：详情侧栏（改名 / 成员 / 画像 / 关系）
│   │   ├── useFamilyGraph.ts               新：取数 + 局部更新
│   │   └── membership.ts                   新：membership 词表与文案
│   └── PeopleView.tsx                      不动
└── styles/family.css                       新
```

**路由与导航不动**：`/people` 已存在，导航项数不变，`router.test.tsx` 与
`pages.smoke.test.tsx` 都无需改。这是裁决③带来的额外好处。

---

## 3.5 从 1.0 逐字搬的常量（`src/people_graph.js:6-23`）

**不要重新发明这三张表**，1.0 的测试（`test/people-graph.test.js`）就是钉它们的。

```js
const relationPriority = new Map([
  ["父亲", 1], ["母亲", 1], ["丈夫", 2], ["妻子", 2],
  ["祖父", 3], ["祖母", 3], ["外祖父", 3], ["外祖母", 3],
  ["哥哥", 4], ["姐姐", 4], ["弟弟", 4], ["妹妹", 4],
  ["叔叔", 5], ["伯伯", 5], ["姑姑", 5], ["舅舅", 5], ["姨妈", 5], ["姨母", 5],
  ["朋友", 8], ["密友", 8], ["其他亲属", 9],
]);
const parentRelations = new Set(["父亲", "母亲", "祖父", "祖母", "外祖父", "外祖母"]);
const partnerRelations = new Set(["丈夫", "妻子"]);
const friendRelations  = new Set(["朋友", "密友"]);

function relationChoices() {
  return [
    ["父亲","儿子"], ["父亲","女儿"], ["母亲","儿子"], ["母亲","女儿"],
    ["丈夫","妻子"], ["妻子","丈夫"], ["哥哥","弟弟"], ["姐姐","妹妹"],
    ["朋友","朋友"], ["密友","密友"], ["其他亲属","其他亲属"],
  ].map(([predicate, inversePredicate]) => ({ label: `${predicate} → ${inversePredicate}`, predicate, inversePredicate }));
}
```

**一对人只画一条边**（`people_graph.js:43-52`）：按无向 pair 归并，
`relationPriority` 小者胜，同优先级按谓词字典序。这是 1.0 的行为，
**代价是「既是亲戚又是同事」时只显示一条**——已知的信息丢失，F7 原样保留，
不在这一段里改语义（要改得先在文档里登记偏离）。

---

## 3.6 `shared/api/client.ts` 要新增的接口（**这是并行施工的契约，签名不要改**）

权威形状在 `GET http://127.0.0.1:9598/openapi.json` 的 `components.schemas`，
`Family*` 那十个。下面是前端要落的 TS：

```ts
export type FamilyMembershipValue = "family" | "friend" | "unknown";

export interface FamilyMembership {
  id: string; scope_id: string; entity_id: string;
  membership: string; source: string; confidence: number;
  evidence_refs: string[]; inference_run_id: string | null;
  state: string; locked: boolean; revision: number;
  supersedes_id: string | null; created_at: string; updated_at: string;
}

export interface FamilyPortrait {
  id: string; scope_id: string; entity_id: string; portrait_text: string;
  evidence_refs: string[]; source: string; inference_run_id: string | null;
  state: string; locked: boolean; revision: number;
  created_at: string; updated_at: string;
}

export interface FamilyRepresentativeMedia {
  id: string; media_type: string; file_name: string;
}

export interface FamilyPerson {
  id: string; scope_id: string; display_name: string;
  membership: FamilyMembership | null;      // 未推断过 → null，不是缺字段
  portrait: FamilyPortrait | null;
  avatar_face_instance_id: string | null;
  appearance_count: number;
  representative_media: FamilyRepresentativeMedia[];
}

export interface FamilyRelationship {
  id: string; scope_id: string; family_graph_id: string | null;
  subject_entity_id: string; predicate: string;
  object_entity_id: string; inverse_predicate: string;
  source: string; confidence: number; evidence_refs: string[];
  state: string; locked: boolean; revision: number;
}

export interface FamilyRun {
  id: string; scope_id: string; status: string; current_stage: string;
  config: Record<string, unknown>; stats: Record<string, unknown>;
  error: string | null;
  created_at: string | null; updated_at: string | null; completed_at: string | null;
}

export interface FamilyGraph {
  scope_id: string; run: FamilyRun | null;
  people: FamilyPerson[]; scope_ids: string[]; relationships: FamilyRelationship[];
}

/** 全部失败都以 ApiError 抛出（既有约定），成功一律 `{ok:true,…}`。 */
export async function getFamilyGraph(scopeId: string): Promise<FamilyGraph>;
export async function setFamilyMembership(
  personId: string, scopeId: string, membership: FamilyMembershipValue,
): Promise<FamilyMembership | null>;
export async function setFamilyPortrait(
  personId: string, scopeId: string, portraitText: string,
): Promise<FamilyPortrait | null>;
export async function putFamilyRelationship(input: {
  scopeId: string; subjectEntityId: string; predicate: string;
  objectEntityId: string; inversePredicate: string;
}): Promise<FamilyRelationship[]>;
export async function deleteFamilyRelationship(input: {
  scopeId: string; subjectEntityId: string; objectEntityId: string;
}): Promise<FamilyRelationship[]>;
export async function mergeFamilyScopes(scopeIds: string[]): Promise<{ graph_id: string; scope_ids: string[] }>;
export async function runFamilyGraph(scopeId: string): Promise<FamilyRun>;
```

**三个容易写错的地方**（都在 openapi 里能核到）：

1. `DELETE /family-graph/relationships` 的体**只有** `scope_id / subject_entity_id / object_entity_id`
   ——**没有 predicate**，删的是这对人的那条边。
2. 响应是 `{ok, relationships}` / `{ok, membership}` / `{ok, portrait}`，**不是裸对象**。
3. `avatar_face_instance_id` 非空时头像走 `/api/v1/face-instances/{id}/crop`
   （`PeopleView` 里已有同样写法，照它取）。

---

## 4. 组件契约

### `useFamilyGraph(scopeId)`

唯一取数入口。返回
`{ state, people, relationships, run, scopeIds, reload, refresh }`。
`state ∈ {"loading","ready","empty","error"}`。
所有写操作成功后**重新拉一次**（不做乐观更新——后端是权威，且 LWW/锁语义在服务端）。

### `FamilyRoster`

props `{ people, selectedId, onSelect }`。
本地状态：`query`（搜名字）、`filter ∈ {all, family, friend, unknown}`。
排序：有名在前 → 出现次数降序 → id 升序（**稳定**，不用 localeCompare 的随机性）。
空结果给 `EmptyState`，文案区分「一条都没有」与「筛掉了」。

### `FamilyGraphView`

props `{ people, relationships, selectedId, onSelect, onRelate }`。

- **布局**：`graphLayout(people, relationships, width, height) -> Map<id, {x,y}>`
  - 辈分传播：照 1.0 `people_graph.js:13` 的 `parentRelations = {父亲,母亲,祖父,祖母,外祖父,外祖母}`
    ——subject 是长辈、object 是晚辈，所以 `level[object] = max(level[object], level[subject]+1)`
    （`people_graph.js:73`）。**方向原样保留**：父亲在上、儿子在下。
    1.0 只有 6 个谓词进这个集合（叔伯姑舅姨不在内），照搬，不顺手补全
  - 家人按层分行，**每层按节点数算最小间距**（1.0 是等分，人多了必重叠）
  - **待归类（unknown）单独一行放最下面**，虚线圈 —— 这条是 F7 相对 1.0 的核心改动
  - 朋友一层
  - 纯函数，可单测
- **加载即自适应**：算出包围盒 → 初始 `scale`/`translate` 让全图刚好装下（1.0 的「适配全图」
  只是复位到 `scale=1`，`app.js:1991`）
- **平移缩放**：滚轮 + 拖空白。缩放期间**只改 `transform` 属性，不触发 React 重渲染**
  （1.0 每帧 `renderView()` 重建整页 `innerHTML`，`app.js:1299-1343`）
- **边标签只在选中/悬停时显示**（1.0 常显，`app.js:659` 的「XX的朋友」在节点多时严重重叠）
- 搜索命中的节点高亮、其余降透明度

### `PersonDetail`

props `{ person, relationships, onClose, onChanged }`。
三段：身份（改名 / 成员身份 / 画像）/ 关系（列表 + 删）/ 证据（出现次数 + 代表照片）。
写操作走 client，成功后 `onChanged()`。

---

## 5. 验收

- `make web-check` 全绿（tsc -b + oxlint + vitest + vite build）
- 新增单测：`graphLayout.test.ts`（分层正确性、**unknown 必须出现在结果里**、空图不炸）
- 新增冒烟：`/people` 在「有 8 人 0 关系」这个**真实形状**下必须渲染出 8 张卡，
  而不是空状态——这条直接钉 §0 的结论
- 在 153 上用真实数据实跑一遍，人肉确认

---

## 6. 施工记录

### 6.1 交付

| 提交 | 内容 |
|---|---|
| `43d247c` | 前端主体：`/people` 三个 tab、名录、手写 SVG 图谱、详情侧栏、`family.css` |
| `65bf87e` | 修「全部相册」下看得见改不了（后端 `family_graph_people` 用错 scope）+ 对抗性审查确认的四条 |
| `aa030f5` | 补图谱 tab 的覆盖（把四段数学抽成纯函数）+ 裸 NUL 字节门禁 |

`make web-check`：**61 passed**（原 38 → 49 → 61）。

### 6.2 实测（§0 的依据）

在 153 真实库（53 张照片 / 17 条事件 / 9 个实体）上跑 `POST /family-graph/runs`：

```
8 人 · membership 全 unknown · confidence 全 0.0 · relationships 0 条
```

复现模型调用确认是模型的原始回复。扫全部 53 条观察的 36 个亲属词，
只命中「合影」2 /「家庭成员」1 /「朋友」1，**「父亲/母亲/儿子/女儿/丈夫/妻子/
夫妻/家人/婚礼」零命中**——数据侧证实模型判 unknown 是对的。

对照：1.0 自己的库里有 9 条模型推出的亲子关系（confidence 0.85–0.9）与
34 条 `family` 归属，作用域 `family-plan-album3-video10-20260914`。链路是通的。

**顺带修掉的既有缺陷**（`b067ba1`，不在 F7 范围但是实测的产物）：
2.0 的家庭图谱证据查询漏读了 `place` / `event_type` / `objects_json` /
`clothing_json`（实测 46/53、46/53、46/53、33/53 行有值），1.0 是把这些拼进
`canonical_json` 喂模型的。已补，7/7 定点变异被杀。详见 S7 计划书 §8.5。

### 6.3 偏离登记

| # | 偏离 | 理由 |
|---|---|---|
| 1 | `client.ts` 多一个 `faceInstanceCropUrl(id)` | 该接口不返回 `crop_url`，路径只能前端拼；放 client 里做单一来源，不在组件里散拼 |
| 2 | `PersonDetail` 多 `people` / `relateTarget` 两个 prop；`useFamilyGraph` 多返回 `error` | 前者用于显示「对方是谁」与拖拽预选，后者给页面错误条。四契约签名本身未改 |
| 3 | 「合并相册范围」「重新推断」的入口做在 `PeoplePage` 里 | §1⑤ 把这两个列入写范围、§3.6 给了 client 函数，但 §4 没有对应组件契约 |
| 4 | 把 `fit` / `zoomAt` / `screenToWorld` / `worldToScreen` / `nearestNodeId` 抽成 `graphLayout.ts` 的纯函数 | 它们原本埋在事件处理里，零覆盖。**公式与常量逐字未动**，组件改为调用 |
| 5 | 图谱自带本地搜索框而非从 props 收 `query` | §4 要求「搜索命中高亮」，但 props 契约里没有 query |

### 6.4 已知未修（留给后续）

按对抗性审查的排序，这些**不拦「打开看一眼」，但都成立**：

1. 触屏：`.graph-canvas { touch-action: none }` 挡掉滚动，且只监听 `wheel`，
   没有捏合缩放——手机上图谱只能平移不能缩放。
2. 拖拽建关系时每个 `pointermove` 都 `setRelatePreview`，全量重渲染节点+边+标签。
   缩放的路径已经做到「只改 transform」，拖拽这条没有。
3. 拖拽落点读 `relatePreviewRef`（passive effect 里写），与原生 `pointerup`
   没有顺序保证——快速划过 B、C 后在 C 松手可能连到 B。**未复现，疑似。**
4. 辈分传播遇环不收敛（`graphLayout.ts` 的迭代上限取人数，是兜底不是正确处理）。
   1.0 同样没有处理，属照搬其缺陷。
5. 层数多时行距被压到节点重叠（`rowGap = min(固定值, 可用高度/(层数-1))`）。
6. 对比度：`.graph-hint`（`--faint` 对白底 ≈2.6:1）、`.membership-badge.unknown`
   （≈4.3:1）均低于 AA 的 4.5:1，且都是 10px。
7. 详情侧栏无焦点管理（不移焦点、无 Esc、无焦点陷阱）；tab 只做了半个 WAI-ARIA。
8. 「改关系」只能删了重建（裁决⑤ 列的「改」没有独立路径）。
9. 空画像无法清空——后端 `_required` 硬拒空串，前端只能挡住提交并说明。

> **2026-10-03 更正（W1 期间发现）**：本段 §6.1 记的「`make web-check` 全绿（61 passed）」
> **是无效的**——那条门禁当时跑的是不带 `--force` 的 `tsc -b`，而 **`tsc -b` 不会检查
> 新增的源文件**，所以 `source-hygiene.test.ts` 从未被类型检查过，它带着三个 `TS2591`
> 进了仓库。`Makefile` 已改成 `tsc -b --force`，该文件也已修好。
> **教训不在「忘了跑门禁」，在「门禁自己有一类盲区」**：绿不等于查过。
> 详见 `2026-10-03-w1-wemm-embedding-plan.md` §6。
