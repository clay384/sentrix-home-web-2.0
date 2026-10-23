const BASE = "/api/v1";

export interface AssetSummary {
  id: string;
  file_name: string;
  media_type: string;
  mime_type: string | null;
  size_bytes: number;
  status: string;
  captured_at: string | null;
  gps: { latitude: number; longitude: number } | null;
  /** 关键帧血缘（`GET /assets` 的 `AssetSummary` 会带）。普通资产为 null。 */
  derived_kind?: string | null;
  parent_asset_id?: string | null;
  /** 派生关键帧在源视频里的秒数；用它把 `<video>` 定位到命中帧。 */
  source_timestamp_sec?: number | null;
  /** 可选的外部来源身份及原始元数据；旧服务可能不返回。 */
  external_source_id?: string | null;
  source_timestamp?: string | null;
  source_location?: string;
  source_metadata?: Record<string, unknown>;
  /**
   * 后端**目前不返回**。若将来 `/assets` 带上它，资料库就能按相册聚合；
   * 现在消费方必须按「可能为空」处理，不能把它当成一册一个值。
   */
  scope_id?: string;
}

interface AssetListResponse {
  items: AssetSummary[];
}

/** 后端统一错误体的形状（``apps/api/src/sentrix/api/errors.py``）。 */
interface ApiErrorBody {
  code: string;
  message: string;
  detail: string;
}

/**
 * 带 ``code`` 的接口错误。
 *
 * 按 ``code`` 分支而不是按状态码猜：``code`` 才是后端契约里的字段，状态码只是它的
 * 载体——同一件事的状态码变过（空名字刻意保持 400 而不是 pydantic 的 422），按状态码
 * 写的分支会在那次变更里静默走错。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: string;

  constructor(status: number, body: ApiErrorBody | null, raw: string) {
    super(body === null ? `Sentrix API ${status}: ${raw}` : body.message || body.code);
    this.name = "ApiError";
    this.status = status;
    this.code = body === null ? "unknown" : body.code;
    this.detail = body === null ? raw : body.detail;
  }
}

function parseApiError(raw: string): ApiErrorBody | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed === "object" && parsed !== null && "code" in parsed) {
      return parsed as ApiErrorBody;
    }
  } catch {
    // 非 JSON 的错误体（反向代理的 502 页面之类）原样留在 raw 里，走兜底分支。
  }
  return null;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    const raw = await response.text();
    throw new ApiError(response.status, parseApiError(raw), raw);
  }
  return (await response.json()) as T;
}

export async function listAssets(scopeId: string): Promise<AssetSummary[]> {
  const body = await request<AssetListResponse>(`/assets?scope_id=${encodeURIComponent(scopeId)}`);
  return body.items;
}

export function assetFileUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/file`;
}

export function assetPreviewUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/preview`;
}

/** 浏览器能直接渲染的格式走原图，其余（HEIC 等）走服务端转码预览。 */
export function assetDisplayUrl(asset: AssetSummary): string {
  if (asset.mime_type === "image/heic" || asset.mime_type === "image/heif") {
    return assetPreviewUrl(asset.id);
  }
  return assetFileUrl(asset.id);
}

/** 后端已经按 mime 决定过怎么交付；助手交付的照片只带回这些字段，走同一条判断。 */
export function deliveredPhotoUrl(assetId: string, fileName: string): string {
  const lowered = fileName.toLowerCase();
  if (lowered.endsWith(".heic") || lowered.endsWith(".heif")) {
    return assetPreviewUrl(assetId);
  }
  return assetFileUrl(assetId);
}

export interface SearchHit {
  asset: AssetSummary;
  score: number;
  channels: Record<string, number>;
  raw_scores: Record<string, number>;
}

export interface SearchChannelTrace {
  channel: string;
  status: string;
  candidate_count: number;
  latency_ms: number;
  backend: string;
  reason: string;
}

export interface SearchResponse {
  query: string;
  backend: string;
  degraded: boolean;
  degraded_reason: string;
  authoritative_vectors: number;
  /** 后端会返回；老响应里可能没有，消费方按可选处理。 */
  excluded_count?: number;
  channel_trace?: SearchChannelTrace[];
  items: SearchHit[];
}

/** 按内容词检索。后端负责嵌入、召回与融合，前端不做任何相关性计算。 */
export async function searchAssets(
  query: string,
  scopeId: string,
  limit = 20,
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query, scope_id: scopeId, limit: String(limit) });
  return request<SearchResponse>(`/search?${params.toString()}`);
}

export interface TimelinePhoto {
  asset_id: string;
  file_name: string;
  captured_at: string | null;
  place: string;
}

export interface TimelineEvent {
  id: string;
  title: string;
  event_type: string;
  time_start: string | null;
  time_end: string | null;
  admin_district: string;
  place: string;
  activity: string;
  summary: string;
  confidence: number;
  photos: TimelinePhoto[];
}

export interface TimelineDay {
  date: string;
  events: TimelineEvent[];
}

export interface TimelineResponse {
  scope_id: string;
  total: number;
  offset: number;
  limit: number;
  days: TimelineDay[];
}

/**
 * 时间线。**地点筛选走后端**：前端只把用户输入原样传过去，不解析任何地名。
 *
 * `place` 为空时不传这个参数——「用户没筛」与「用户筛了一个空串」是两件事。
 */
export async function fetchTimeline(
  scopeId: string,
  offset = 0,
  limit = 20,
  place = "",
): Promise<TimelineResponse> {
  const params = new URLSearchParams({
    scope_id: scopeId,
    offset: String(offset),
    limit: String(limit),
  });
  if (place) {
    params.set("place", place);
  }
  return request<TimelineResponse>(`/timeline?${params.toString()}`);
}

/**
 * 单个事件。与 `/timeline` 里的一项**是同一个模型**（后端 `_event_payload`），
 * 没有额外的人物 / 事实 / Observation 字段——详情页只展示实际存在的这些。
 */
export async function fetchEvent(eventId: string): Promise<TimelineEvent> {
  return request<TimelineEvent>(`/events/${encodeURIComponent(eventId)}`);
}
// ---- 助手 -------------------------------------------------------------------

export interface ConversationSummary {
  id: string;
  scope_id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message_at: string;
}

export interface AssistantProgressEvent {
  text: string;
  status: string;
  stage: string;
  step_index: number;
  timestamp: string;
  kind: string;
  payload: Record<string, unknown>;
}

export interface AssistantAttachment {
  handle: string;
  asset_id: string;
  file_name: string;
}

export interface AssistantRequirement {
  id: string;
  evidence_type: string;
  description: string;
  status: string;
  satisfied_by: string[];
  unmet_reason: string;
}

export interface AssistantCompleteEvent {
  type: string;
  turn_id: string;
  status: string;
  termination_reason: string;
  answer: string;
  attachments: AssistantAttachment[];
  requirements: AssistantRequirement[];
  error: string;
}

export async function createConversation(scopeId: string): Promise<ConversationSummary> {
  return request<ConversationSummary>("/assistant/conversations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_id: scopeId }),
  });
}

/** 会话里的一条消息（`GET /assistant/conversations/{id}` 的 `messages[]`）。 */
export interface ConversationMessage {
  id: string;
  role: string;
  content: string;
  turn_id: string;
  created_at: string;
}

/** 会话里的一个回合（同一条详情响应里的 `turns[]`）。 */
export interface ConversationTurn {
  id: string;
  question: string;
  status: string;
  termination_reason: string;
  answer: string;
  attachments: AssistantAttachment[];
  created_at: string;
}

/**
 * 会话详情：**消息与回合一起回来**。
 *
 * 历史回放读 `messages`（顺序由后端按 `created_at, rowid` 排好），用 `turn_id` 把
 * 每条 assistant 消息与 `turns[]` 里的附件对上——交付的照片只存在于回合那一侧。
 */
export interface ConversationDetail {
  conversation: ConversationSummary;
  messages: ConversationMessage[];
  turns: ConversationTurn[];
}

export async function getConversation(conversationId: string): Promise<ConversationDetail> {
  return request<ConversationDetail>(
    `/assistant/conversations/${encodeURIComponent(conversationId)}`,
  );
}

/** 改会话标题。后端返回改名后的会话（`PATCH` 的响应不是 204）。 */
export async function renameConversation(
  conversationId: string,
  title: string,
): Promise<ConversationSummary> {
  return request<ConversationSummary>(
    `/assistant/conversations/${encodeURIComponent(conversationId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    },
  );
}

/**
 * 删除会话。后端回 204 且**没有响应体**，所以不能走 `request<T>` 的 `json()`。
 * 错误体仍然按统一契约解析，调用方拿到的还是 `ApiError`。
 */
export async function deleteConversation(conversationId: string): Promise<void> {
  const response = await fetch(
    `${BASE}/assistant/conversations/${encodeURIComponent(conversationId)}`,
    { method: "DELETE" },
  );
  if (!response.ok) {
    const raw = await response.text();
    throw new ApiError(response.status, parseApiError(raw), raw);
  }
}

/**
 * 建一个回合。**不带 scope**：回合属于会话，scope 由后端从会话取。
 *
 * 前端曾经一路把 ``scopeId`` 传到这里。那是把「会话的 scope」这个后端事实又抄了一遍，
 * 抄错的时候（省略、传旧值）后端会静默按错的 scope 检索，用户看到的是「搜不到」。
 */
export async function startTurn(
  conversationId: string,
  message: string,
): Promise<{ turn_id: string; conversation_id: string; status: string }> {
  return request(`/assistant/conversations/${conversationId}/turns`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
}

/** 回合事件的 SSE 地址。浏览器侧的主路径，与 1.0 ``src/api.js:66`` 同形。 */
export function turnEventsUrl(turnId: string): string {
  return `${BASE}/assistant/turns/${turnId}/events`;
}

/**
 * 取一个**已结束**回合的过程事件（历史回放的推理时间线）。
 *
 * 后端只有 SSE 这一条事件出口：`GET /assistant/turns/{id}/events` 会先把库里
 * 已经落盘的事件逐条发完，再补一个 `complete` 帧然后关流。所以这里用
 * `EventSource` 收完就 resolve，不是轮询。
 *
 * 只该对终态回合调用——进行中的回合那条流不会关，超时是兜底而不是正常路径。
 */
export function fetchTurnEvents(turnId: string, timeoutMs = 5000): Promise<AssistantProgressEvent[]> {
  return new Promise((resolve) => {
    const events: AssistantProgressEvent[] = [];
    const source = new EventSource(turnEventsUrl(turnId));
    let settled = false;
    const finish = () => {
      if (settled) return;
      settled = true;
      source.close();
      resolve(events);
    };
    source.addEventListener("progress", (event) => {
      try {
        events.push(JSON.parse((event as MessageEvent<string>).data) as AssistantProgressEvent);
      } catch {
        // 坏帧跳过：一条解析不了的进度不该让整条时间线消失。
      }
    });
    source.addEventListener("complete", finish);
    // 服务端发完 complete 就关流，浏览器仍会触发 onerror；finish 有幂等闩。
    source.onerror = finish;
    setTimeout(finish, timeoutMs);
  });
}

export interface AssistantTurnPoll {
  turn_id: string;
  status: string;
  public_progress: AssistantProgressEvent[];
  result: AssistantCompleteEvent | null;
  error: string;
}

/**
 * 轮询一个回合的状态——1.0 ``src/api.js:64`` 的 ``assistantTurnPoll``，同一条契约。
 *
 * 浏览器侧只在 SSE 断掉之后用它（``useTurnStream``，与 1.0 ``src/app.js:1482`` 的
 * 「优先 SSE、EventSource 不可用时回退」同一个顺序）。它同时也是 ``EventSource``
 * 用不了的调用方的唯一一条路——1.0 的 ``services/photobench`` 就是靠它跑完每个回合的
 * （``benchmark_orchestrator.py:881`` 每 0.5 秒一次，8091 日志里 668,552 次）。
 */
export async function pollTurn(turnId: string): Promise<AssistantTurnPoll> {
  return request<AssistantTurnPoll>(`/assistant/turns/${encodeURIComponent(turnId)}`);
}

/**
 * 结果集授权原图 URL——1.0 ``src/api.js:67`` 的 ``resultSetPhoto``，同一条契约。
 *
 * URL 里只有结果集 id 与 ``photo_N`` 这个 handle，**没有 asset_id**。``photo_N`` 由
 * 结果集自己做下标解析，越界的 handle 解析不出来——「只交付模型真的看过的图」因此是
 * 结构性事实，而不是提示词里的一句请求。
 */
export function resultSetPhotoUrl(
  resultSetId: string,
  handle: string,
  scopeId: string,
  original = false,
): string {
  const query = new URLSearchParams({ handle, scope_id: scopeId });
  if (original) {
    query.set("original", "1");
  }
  return `${BASE}/assistant/result-sets/${encodeURIComponent(resultSetId)}/photo?${query}`;
}

// ---- 身份 / 人脸簇 ---------------------------------------------------------------

/**
 * 簇里的一张样本脸。
 *
 * ``crop_url`` **是服务端拼好的整串**（``schemas/identity.py`` 的类注释）：前端只负责
 * 把它塞进 ``<img src>``，不再自己拼 ``/api/v1/face-instances/{id}/crop``。1.0 两个地方
 * 各拼一次，路径一改就要同时改两处。
 */
export interface FaceCropRef {
  face_instance_id: string;
  asset_id: string;
  observation_id: string;
  file_name: string;
  media_type: string;
  bbox: number[];
  detection_confidence: number;
  quality: number;
  crop_url: string;
}

/**
 * 一簇脸。``canonical_name`` 与 ``family_role`` 为空是**未命名**，不是缺字段。
 *
 * ``reviewable`` / ``single_sample`` 是**服务端算好的派生布尔**（1.0 ``db.py:5146`` 的
 * 同一对）：能不能审、是不是只有一张脸的孤证。前端据此决定按钮可用状态，不重算规则。
 */
export interface FaceClusterSummary {
  id: string;
  scope_id: string;
  status: string;
  entity_id: string | null;
  canonical_name: string | null;
  family_role: string | null;
  entity_status: string | null;
  member_count: number;
  confidence: number;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
  reviewable: boolean;
  single_sample: boolean;
  samples: FaceCropRef[];
}

/** 一个人物实体。``confirmed`` 与 ``status`` 并存是 1.0 的契约，两个都留着。 */
export interface PersonPayload {
  id: string;
  scope_id: string;
  canonical_name: string;
  status: string;
  family_role: string | null;
  summary: string;
  confidence: number;
  confirmed: boolean;
  aliases: string[];
}

interface FaceClusterListResponse {
  clusters: FaceClusterSummary[];
}

interface PersonListResponse {
  people: PersonPayload[];
}

/** ``merged_into`` 非空表示这次确认并进了另一个已存在的人（同名合并）。 */
export interface ConfirmClusterResponse {
  entity: PersonPayload;
  merged_into: string | null;
}

/** ``rejected_entity_id`` 为空表示这一簇本来就没有候选实体，调用没有改动任何行。 */
export interface RejectClusterResponse {
  cluster_id: string;
  rejected_entity_id: string | null;
}

export interface PersonDetailResponse {
  entity: PersonPayload;
  avatar_face_instance_id: string | null;
  clusters: FaceClusterSummary[];
}

export interface RenamePersonResponse {
  entity: PersonPayload;
  aliases: string[];
}

export interface BatchConfirmItem {
  person_id: string;
  name: string;
  family_role?: string | null;
}

export interface BatchConfirmOutcome {
  person_id: string;
  entity_id: string;
  name: string;
  family_role: string | null;
  merged: boolean;
}

export interface BatchConfirmFailure {
  person_id: string;
  name: string;
  error: string;
}

/** 批量确认**不做全或无**：一件失败不影响其余，逐件回执。 */
export interface BatchConfirmResponse {
  confirmed: BatchConfirmOutcome[];
  failed: BatchConfirmFailure[];
  count: number;
}

/**
 * 簇列表。**默认不含 rejected**——后端 ``list_clusters(status=None)`` 就是这个语义，
 * 前端不自己过滤。传 ``status`` 再切一刀。
 */
export async function listFaceClusters(
  scopeId: string,
  status?: string,
): Promise<FaceClusterSummary[]> {
  const params = new URLSearchParams({ scope_id: scopeId });
  if (status) {
    params.set("status", status);
  }
  const body = await request<FaceClusterListResponse>(`/face-clusters?${params.toString()}`);
  return body.clusters;
}

/**
 * 确认一簇脸，给它一个名字。``name`` 允许为空串——空名字是**用例的判据**（400），
 * 不是请求格式错，所以这里不替后端拦。
 */
export async function confirmFaceCluster(
  clusterId: string,
  payload: { name: string; family_role?: string | null },
): Promise<ConfirmClusterResponse> {
  return request<ConfirmClusterResponse>(`/face-clusters/${encodeURIComponent(clusterId)}/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: payload.name, family_role: payload.family_role ?? null }),
  });
}

/** 拒绝一簇：删掉候选实体，原始照片不受影响。 */
export async function rejectFaceCluster(clusterId: string): Promise<RejectClusterResponse> {
  return request<RejectClusterResponse>(`/face-clusters/${encodeURIComponent(clusterId)}/reject`, {
    method: "POST",
  });
}

/**
 * 合并两个簇：``source`` 的样本并进 ``target``，返回合并后的 ``target``。
 *
 * 两个 id 都由调用方的多选给出，方向由调用方指定——后端不做「哪个更像谁」的推断。
 */
export async function mergeFaceClusters(
  targetClusterId: string,
  sourceClusterId: string,
): Promise<FaceClusterSummary> {
  return request<FaceClusterSummary>("/face-clusters/merge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      target_cluster_id: targetClusterId,
      source_cluster_id: sourceClusterId,
    }),
  });
}

/** 从一簇里拆出一张脸，返回**新建的那个簇**（不是原簇）。 */
export async function splitFaceCluster(
  clusterId: string,
  faceInstanceId: string,
): Promise<FaceClusterSummary> {
  return request<FaceClusterSummary>(`/face-clusters/${encodeURIComponent(clusterId)}/split`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ face_instance_id: faceInstanceId }),
  });
}

export async function listPeople(scopeId: string, status?: string): Promise<PersonPayload[]> {
  const params = new URLSearchParams({ scope_id: scopeId });
  if (status) {
    params.set("status", status);
  }
  const body = await request<PersonListResponse>(`/people?${params.toString()}`);
  return body.people;
}

export async function getPerson(personId: string): Promise<PersonDetailResponse> {
  return request<PersonDetailResponse>(`/people/${encodeURIComponent(personId)}`);
}

/** 改一个人的名字。返回里带 ``aliases``——旧名字会留成别名，前端不自己拼这份历史。 */
export async function renamePerson(personId: string, name: string): Promise<RenamePersonResponse> {
  return request<RenamePersonResponse>(`/people/${encodeURIComponent(personId)}/name`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export async function batchConfirmPeople(
  items: BatchConfirmItem[],
): Promise<BatchConfirmResponse> {
  return request<BatchConfirmResponse>("/people/batch-confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items }),
  });
}

/**
 * 写一条实体属性。**别名走的就是这条路**：仓储把 ``aliases`` 存成
 * ``entity_properties`` 的一行（``ALIAS_PROPERTY_KEY``），``PATCH /people/{id}/name``
 * 并不接受 aliases，所以别名编辑要用这条通用属性写入。
 *
 * 响应是属性行，本模块没有消费方，因此不建模——调用方只关心「写成功没有」，失败由
 * ``request`` 抛 ``ApiError``。
 */
export async function setEntityProperty(
  entityId: string,
  propertyKey: string,
  value: unknown,
): Promise<void> {
  await request(
    `/entities/${encodeURIComponent(entityId)}/properties/${encodeURIComponent(propertyKey)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    },
  );
}

// ---- 人物证据 -------------------------------------------------------------------

export interface EntityDetailAsset {
  id: string;
  file_name: string;
  media_type: string;
  path: string;
}

export interface EntityDetailObservation {
  id: string;
  asset_id: string;
  source_type: string;
  captured_at: string | null;
  caption: string;
  asset: EntityDetailAsset | null;
}

export interface EntityDetailEvent {
  id: string;
  scope_id: string;
  title: string;
  event_type: string;
  time_start: string | null;
  time_end: string | null;
  place: string;
  admin_district: string;
  activity: string;
  summary: string;
  confidence: number;
  aggregation_score: number;
  status: string;
  revision: number;
}

export interface EntityDetailEntity {
  id: string;
  scope_id: string;
  canonical_name: string;
  status: string;
  family_role: string | null;
  summary: string;
  identity_state: string;
  role_state: string;
  name_state: string;
  avatar_face_instance_id: string | null;
  evidence_count: number;
}

/**
 * 实体详情。人物证据页用它取**关联事件**——``GET /people/{id}`` 只返回人物与簇，
 * 没有事件；事件在 ``GET /entities/{id}`` 的 ``events`` 里。
 *
 * 只建模本页消费的三块（``entity`` / ``clusters`` / ``events``）；响应的属性、事实、
 * 观察等块不在本页使用，不重复声明。
 */
export interface EntityDetailResponse {
  entity: EntityDetailEntity;
  clusters: FaceClusterSummary[];
  events: EntityDetailEvent[];
  observations: EntityDetailObservation[];
}

export async function getEntityDetail(entityId: string): Promise<EntityDetailResponse> {
  return request<EntityDetailResponse>(`/entities/${encodeURIComponent(entityId)}`);
}

// ---- 人物画像 -------------------------------------------------------------------

export interface PortraitRevision {
  id: string;
  person_id: string;
  revision: number;
  portrait_text: string;
  themes: string[];
  evidence_refs: string[];
  status: string;
  trigger_type: string;
  model_name: string;
  prompt_version: string;
  feedback: string | null;
}

export interface PersonInsightItem {
  person_id: string;
  display_name: string;
  family_role: string | null;
  status: string | null;
  portrait: PortraitRevision | null;
  [key: string]: unknown;
}

export interface PersonInsightTiers {
  core: PersonInsightItem[];
  common: PersonInsightItem[];
  incidental: PersonInsightItem[];
}

export interface PersonInsightsResponse {
  scope_id: string;
  tiers: PersonInsightTiers;
}

/** 人物按核心度分档，各带当前生效的画像。``scope_id`` 为空时后端兜底默认空间。 */
export async function listPersonInsights(scopeId: string): Promise<PersonInsightsResponse> {
  const params = new URLSearchParams();
  if (scopeId) {
    params.set("scope_id", scopeId);
  }
  return request<PersonInsightsResponse>(`/person-insights?${params.toString()}`);
}

/**
 * 对**当前生效那一版画像**记一条反馈。
 *
 * 2.0 的请求体只有 ``feedback``（``schemas/insights.py`` 的 ``PortraitFeedbackRequest``），
 * 不是 1.0 的 ``{revision_id, verdict, note}``。因此「像他 / 不像他」落成 feedback
 * 文本本身；**锁定没有对应端点**（2.0 没有 PATCH portrait），不在前端伪造。
 */
export async function sendPortraitFeedback(
  personId: string,
  feedback: string | null,
): Promise<PortraitRevision> {
  return request<PortraitRevision>(
    `/people/${encodeURIComponent(personId)}/portrait-feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ feedback }),
    },
  );
}

/**
 * 本地服务健康。字段就是后端 `/health` 的五个
 * （`apps/api/src/sentrix/api/routes/health.py`）。
 *
 * `embeddings` 是 2026-10-10 PhotoBench 那轮「嵌入侧车全程不在、接口一直回 ok」
 * 之后补的：数据库好、嵌入器坏不是 `ok`。`embeddings_error` 只在不可用时出现。
 *
 * **没有模型端点字段。** 页面要展示「模型端点」时只能如实说明它没有暴露，
 * 不能从别处猜一个填进去。
 */
export interface HealthResponse {
  status: string;
  version: string;
  database: string;
  embeddings: string;
  embeddings_error?: string;
}

export async function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

/**
 * 记忆空间（相册）列表。
 *
 * 形状对齐 1.0 的消费处 `src/app.js:1161`：`spaceResult[0].value.spaces || []`，
 * 即后端给 `{spaces: [...]}`。四个字段就是 `memory_spaces` 的四列，没有派生字段。
 */
export interface MemorySpace {
  id: string;
  name: string;
  kind: string;
  status: string;
}

interface MemorySpaceListResponse {
  spaces: MemorySpace[];
}

export async function listMemorySpaces(): Promise<MemorySpace[]> {
  const body = await request<MemorySpaceListResponse>("/memory-spaces");
  return body.spaces;
}

/** 导入的响应体（`apps/api/src/sentrix/api/schemas/media.py` 的 `ImportResponse`）。 */
export interface ImportResponse {
  asset_id: string;
  observation_id: string | null;
  media_type: string;
  status: string;
  deduplicated: boolean;
  /** 有没有真的写成视觉向量。1.0 的「导入成功但没有向量」在接口上完全不可见。 */
  indexed: boolean;
  /** 有没有解析出地点（落 `asset_places` 一行）。 */
  placed?: boolean;
  /** 折进 / 新建的事件 id；事件归属没接上时为 null。 */
  event_id?: string | null;
  /** 视频有没有真的走到 ffprobe。图片恒为 false。 */
  video_probed?: boolean;
  /** 这次导入之后建起来的视频场景数。图片恒为 0。 */
  video_scenes?: number;
  /** 音频有没有真的走到 ffprobe。音频以外恒为 false。 */
  audio_probed?: boolean;
}

/**
 * 导入一个文件。`POST /api/v1/media/import`。
 *
 * 用 multipart：`file` 是文件本身、`scope_id` 是表单字段。**不要手写 Content-Type**——
 * 浏览器要给 multipart 带上 boundary，手写会把 boundary 弄丢。
 */
export async function importAsset(file: File, scopeId: string): Promise<ImportResponse> {
  const form = new FormData();
  form.append("file", file);
  form.append("scope_id", scopeId);
  return request<ImportResponse>("/media/import", { method: "POST", body: form });
}

/** 某个相册范围下的会话列表。`GET /api/v1/assistant/conversations`。 */
export async function listConversations(scopeId: string): Promise<ConversationSummary[]> {
  const body = await request<{ items: ConversationSummary[] }>(
    `/assistant/conversations?scope_id=${encodeURIComponent(scopeId)}`,
  );
  return body.items;
}

/** 全局重聚类的返回体（`api/schemas/maintenance.py` 的 `ReclusterFacesResponse`）。 */
export interface ReclusterFacesResponse {
  instances: number;
  clusters: number;
  noise: number;
  attached_uncertain: number;
  retired_clusters: number;
  retired_candidates: number;
  threshold: number;
  minimum_quality: number;
}

/**
 * 全局重聚类。`POST /api/v1/maintenance/recluster-faces`。
 *
 * `scopeId` 传 `null` 表示**跨空间**重算。那不是「默认空间」——两者在两处不同：
 * 跨空间时 `list_verified_instances` 不按 asset 的 scope 过滤，而认领不确定人脸时
 * 按每条实例自己的空间找目标（schema 的 docstring 写明了这一点）。
 */
export async function reclusterFaces(scopeId: string | null): Promise<ReclusterFacesResponse> {
  return request<ReclusterFacesResponse>("/maintenance/recluster-faces", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_id: scopeId }),
  });
}

// ---- 家庭图谱（F7） ---------------------------------------------------------------
//
// 六个端点由 S7b1/S7b2 上线（`apps/api/src/sentrix/api/routes/family.py`）。形状按
// `GET /openapi.json` 的 `components.schemas` 落。**三处与直觉相反的地方**，都在各自
// 函数上写明：写路由的响应包了 `{ok, …}`、DELETE 的体里**没有** predicate、
// POST runs 包了 `{run}`（1.0 是 `{status, run}`，2.0 裁决成同步后去掉了 status）。

/** 家庭归属。`unknown` 与 `membership === null` 是同一件事：还没被推断归类过。 */
export type FamilyMembershipValue = "family" | "friend" | "unknown";

export interface FamilyMembership {
  id: string;
  scope_id: string;
  entity_id: string;
  membership: string;
  source: string;
  confidence: number;
  evidence_refs: string[];
  inference_run_id: string | null;
  state: string;
  locked: boolean;
  revision: number;
  supersedes_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface FamilyPortrait {
  id: string;
  scope_id: string;
  entity_id: string;
  portrait_text: string;
  evidence_refs: string[];
  source: string;
  inference_run_id: string | null;
  state: string;
  locked: boolean;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface FamilyRepresentativeMedia {
  id: string;
  media_type: string;
  file_name: string;
}

/**
 * `people[]` 的一项。
 *
 * `membership` / `portrait` 是**行对象或 null**（未推断过是 null，不是缺字段）——
 * 前端必须按对象取 `.membership`，把它当标量读会在真实数据上拿到 undefined。
 */
export interface FamilyPerson {
  id: string;
  scope_id: string;
  display_name: string;
  membership: FamilyMembership | null;
  portrait: FamilyPortrait | null;
  avatar_face_instance_id: string | null;
  appearance_count: number;
  representative_media: FamilyRepresentativeMedia[];
}

export interface FamilyRelationship {
  id: string;
  scope_id: string;
  family_graph_id: string | null;
  subject_entity_id: string;
  predicate: string;
  object_entity_id: string;
  inverse_predicate: string;
  source: string;
  confidence: number;
  evidence_refs: string[];
  state: string;
  locked: boolean;
  revision: number;
}

export interface FamilyRun {
  id: string;
  scope_id: string;
  status: string;
  current_stage: string;
  config: Record<string, unknown>;
  stats: Record<string, unknown>;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
  completed_at: string | null;
}

export interface FamilyGraph {
  scope_id: string;
  run: FamilyRun | null;
  people: FamilyPerson[];
  scope_ids: string[];
  relationships: FamilyRelationship[];
}

/** 写路由统一包了 `{ok: true, …}`，不是裸对象——照 1.0 的 `{"ok": True, …}`。 */
interface FamilyMembershipResponse {
  ok: boolean;
  membership: FamilyMembership | null;
}

interface FamilyPortraitResponse {
  ok: boolean;
  portrait: FamilyPortrait | null;
}

interface FamilyRelationshipResponse {
  ok: boolean;
  relationships: FamilyRelationship[];
}

/** 撤回返回的是**撤回之后**的行（`state = "retracted"`），键名是 `retracted`。 */
interface FamilyRetractResponse {
  ok: boolean;
  retracted: FamilyRelationship[];
}

/** `POST /family-graph/runs` 同步跑完再回，体是 `{run}`——不是 1.0 的 `{status: 202, run}`。 */
interface FamilyRunResponse {
  run: FamilyRun;
}

export async function getFamilyGraph(scopeId: string): Promise<FamilyGraph> {
  return request<FamilyGraph>(`/family-graph?scope_id=${encodeURIComponent(scopeId)}`);
}

/**
 * 人脸裁剪图。
 *
 * `FamilyPerson` 只给 `avatar_face_instance_id`，整条路径由前端拼；`PeopleView` 那条
 * 路是服务端把 `crop_url` 拼好带回来的（见 `FaceCropRef` 的注释）。两条路都留着：
 * 家庭图谱的响应里没有 crop_url 这个字段。
 */
export function faceInstanceCropUrl(faceInstanceId: string): string {
  return `${BASE}/face-instances/${encodeURIComponent(faceInstanceId)}/crop`;
}

/** 设一个人的家庭归属。用户按下的动作，后端写死 `source=user_override`、`locked=1`。 */
export async function setFamilyMembership(
  personId: string,
  scopeId: string,
  membership: FamilyMembershipValue,
): Promise<FamilyMembership | null> {
  const body = await request<FamilyMembershipResponse>(
    `/family-graph/people/${encodeURIComponent(personId)}/membership`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope_id: scopeId, membership }),
    },
  );
  return body.membership;
}

/** 手写一段家庭画像。**与人物画像那套不是一张表**，不要串到别的端点上。 */
export async function setFamilyPortrait(
  personId: string,
  scopeId: string,
  portraitText: string,
): Promise<FamilyPortrait | null> {
  const body = await request<FamilyPortraitResponse>(
    `/family-graph/people/${encodeURIComponent(personId)}/portrait`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope_id: scopeId, portrait_text: portraitText }),
    },
  );
  return body.portrait;
}

/** 建一对亲属边。后端一次落**两行**（正行 + 反行），所以回来的是列表。 */
export async function putFamilyRelationship(input: {
  scopeId: string;
  subjectEntityId: string;
  predicate: string;
  objectEntityId: string;
  inversePredicate: string;
}): Promise<FamilyRelationship[]> {
  const body = await request<FamilyRelationshipResponse>("/family-graph/relationships", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      scope_id: input.scopeId,
      subject_entity_id: input.subjectEntityId,
      predicate: input.predicate,
      object_entity_id: input.objectEntityId,
      inverse_predicate: input.inversePredicate,
    }),
  });
  return body.relationships;
}

/**
 * 撤回一对亲属边。
 *
 * **体里只有 scope + 两个实体 id，没有 predicate**——删的是这对人之间的那条边，
 * 不是某一条谓词。1.0 就是这么传的（两个 id 塞不进路径，所以 DELETE 带 body）。
 */
export async function deleteFamilyRelationship(input: {
  scopeId: string;
  subjectEntityId: string;
  objectEntityId: string;
}): Promise<FamilyRelationship[]> {
  const body = await request<FamilyRetractResponse>("/family-graph/relationships", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      scope_id: input.scopeId,
      subject_entity_id: input.subjectEntityId,
      object_entity_id: input.objectEntityId,
    }),
  });
  return body.retracted;
}

/** 把若干 scope 并成一张图。**响应没有 `ok` 键**，1.0 直接返回仓储那个 dict。 */
export async function mergeFamilyScopes(
  scopeIds: string[],
): Promise<{ graph_id: string; scope_ids: string[] }> {
  return request<{ graph_id: string; scope_ids: string[] }>("/family-graph/scopes/merge", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_ids: scopeIds }),
  });
}

/** 手工跑一趟家庭图谱推断。同步返回跑完的 run；已有 run 在跑时后端回 409。 */
export async function runFamilyGraph(scopeId: string): Promise<FamilyRun> {
  const body = await request<FamilyRunResponse>("/family-graph/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scope_id: scopeId }),
  });
  return body.run;
}

// ---- 资产详情 / 音视频交付 ---------------------------------------------------------

/**
 * 详情里内联查看/播放一个资产：视频与音频走 `/preview`，图片走展示 URL。
 *
 * `/assets/{id}/preview` 对视频/音频会落到 `deliver_media`（有转码件给转码件，
 * 否则回落原片），对图片给服务端转码的 JPEG。**不要在 `<video>`/`<audio>` 上
 * 自己拼 `/file`**：原片可能是 HEVC/HEIC，浏览器放不了。
 */
export function assetInlineUrl(asset: AssetSummary): string {
  if (asset.media_type === "video" || asset.media_type === "audio") {
    return assetPreviewUrl(asset.id);
  }
  return assetDisplayUrl(asset);
}

/**
 * 一条 Observation。**形状照 `api/schemas/identity.py`**：`GET /entities/{id}` 与
 * `GET /entity-groups/{id}` 给的就是这一份。`asset` 读不回来时为 null。
 */
export interface ObservationPayload {
  id: string;
  asset_id: string;
  source_type: string;
  captured_at: string | null;
  caption: string;
  asset: EntityDetailAsset | null;
}

/**
 * `GET /assets/{id}`。**后端目前只返回 `AssetSummary`，没有 observations**。
 * 这里用交叉类型兜住未来可能多出来的字段：有就显示，没有就如实说明，
 * 前端不会自己编一条 Observation。
 */
export type AssetDetailResponse = AssetSummary & {
  observations?: ObservationPayload[];
};

export async function getAsset(assetId: string): Promise<AssetDetailResponse> {
  return request<AssetDetailResponse>(`/assets/${encodeURIComponent(assetId)}`);
}

// ---- 视频 ------------------------------------------------------------------------

export interface VideoMetadataPayload {
  captured_at: string | null;
  latitude: number | null;
  longitude: number | null;
  captured_location: string | null;
  duration_sec: number;
  fps: number;
  width: number;
  height: number;
  codec: string;
  rotation: number;
  device: string;
  creation_source: string;
  frame_count: number;
}

/**
 * `GET /videos/{id}` 的 `video` 字段。后端会返回全部字段；这里只把 id / 文件名 /
 * 状态 / 拍摄时间设为必填，其余可选——导入页只消费 `scenes`，它的测试假件不必
 * 为一个不读的字段编值。
 */
export interface VideoPayload {
  id: string;
  file_name: string;
  status: string;
  captured_at: string | null;
  scope_id?: string;
  media_type?: string;
  mime_type?: string | null;
  size_bytes?: number;
  captured_location?: string | null;
  gps?: { latitude: number; longitude: number } | null;
  metadata?: VideoMetadataPayload | null;
  browser_preview_path?: string;
  browser_preview_error?: string;
  probe_error?: string;
  processing_seconds?: number | null;
}

export interface VideoScenePayload {
  id: string;
  scope_id: string;
  title: string;
  event_type: string;
  time_start: string | null;
  time_end: string | null;
  place: string;
  activity: string;
  summary: string;
  confidence: number;
  status: string;
  source_type: string;
  source_asset_id: string;
  source_scene_index: number | null;
  source_start_sec: number | null;
  source_end_sec: number | null;
}

export interface VideoDetailResponse {
  video: VideoPayload;
  scenes: VideoScenePayload[];
}

/** `GET /videos/{id}`：源视频详情 + 场景列表（`api/schemas/videos.py`）。 */
export async function getVideo(assetId: string): Promise<VideoDetailResponse> {
  return request<VideoDetailResponse>(`/videos/${encodeURIComponent(assetId)}`);
}

// ---- 知识 ------------------------------------------------------------------------

/** `GET /knowledge` 的 `profiles[]`（`api/schemas/knowledge.py`）。 */
export interface KnowledgeProfile {
  id: string;
  scope_id: string;
  person_id: string;
  summary_zh: string;
  activity_summary_zh: string;
  place_summary_zh: string;
  appearance_summary_zh: string;
  preference_summary_zh: string;
  event_memory: unknown[];
  patterns: unknown[];
  evidence_ids: string[];
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

/** `GET /knowledge` 的 `claims[]`。`person_id` 指实体 id，不一定是人。 */
export interface KnowledgeClaim {
  id: string;
  scope_id: string;
  person_id: string;
  dimension: string;
  predicate: string;
  value_text: string;
  normalized_value_text: string | null;
  normalization_version: string;
  support_count: number;
  value_entity_id: string | null;
  valid_from: string | null;
  valid_to: string | null;
  supporting_event_ids: string[];
  evidence_ids: string[];
  status: string;
  confidence: number;
  confidence_source: string;
  supersedes_claim_id: string | null;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeSpace {
  id: string;
  name: string;
  kind: string;
  status: string;
}

/**
 * `GET /knowledge` 的三块：`profiles` / `claims` / `spaces`。
 *
 * **没有地点分组**。1.0 的 `state.geoPlaces`（城市 → 区县 → 照片墙）在前端来自另一条
 * 读法；2.0 的 `/knowledge` 没有对应字段，所以知识页不能凭空造一棵地点树。
 */
export interface KnowledgeResponse {
  profiles: KnowledgeProfile[];
  claims: KnowledgeClaim[];
  spaces: KnowledgeSpace[];
}

export async function fetchKnowledge(scopeId: string): Promise<KnowledgeResponse> {
  const params = new URLSearchParams();
  if (scopeId) {
    params.set("scope_id", scopeId);
  }
  const suffix = params.toString();
  return request<KnowledgeResponse>(`/knowledge${suffix ? `?${suffix}` : ""}`);
}

// ---- 实体 / 语义实体组 -------------------------------------------------------------

/** `GET /entities` 的一行（`api/schemas/identity.py` 的 `EntitySummaryPayload`）。 */
export interface EntitySummaryPayload {
  id: string;
  scope_id: string;
  entity_type: string;
  canonical_name: string;
  status: string;
  family_role: string | null;
  summary: string;
  confidence: number;
  identity_state: string;
  role_state: string;
  name_state: string;
  created_at: string | null;
  updated_at: string | null;
  cluster_count: number;
  photo_count: number;
  mention_count: number;
  evidence_count: number;
  relationship_count: number;
  reviewable: boolean;
  single_sample: boolean | null;
  avatar_face_instance_id: string | null;
  preview_asset_id: string | null;
  preview_file_name: string | null;
  preview_media_type: string | null;
  private: boolean | null;
}

export interface EntityGroupRationale {
  strategy: string;
  normalized_key: string;
}

/** 一个语义实体组。**现算的投影，不落表**；单成员组也会返回。 */
export interface EntityGroupPayload {
  id: string;
  scope_id: string;
  entity_type: string;
  canonical_name: string;
  members: EntitySummaryPayload[];
  member_entity_ids: string[];
  source_labels: string[];
  semantic_details: string[];
  evidence_count: number;
  confidence: number;
  preview_asset_id: string | null;
  preview_file_name: string | null;
  preview_media_type: string | null;
  rationale: EntityGroupRationale;
  is_semantic_cluster: boolean;
}

/** `GET /entities/{id}` 的 `entity` 那一块。**与人物证据页那份不同**：
 * 这一份要 `entity_type` 与 `confidence`（地点属性编辑按类型分派），所以不能复用。 */
export interface EntityDetailEntityFull {
  id: string;
  scope_id: string;
  entity_type: string;
  canonical_name: string;
  status: string;
  family_role: string | null;
  summary: string;
  confidence: number;
  identity_state: string;
  role_state: string;
  name_state: string;
  created_at: string | null;
  updated_at: string | null;
  avatar_face_instance_id: string | null;
  evidence_count: number;
}

export interface EntityPropertyPayload {
  id: string;
  entity_id: string;
  property_key: string;
  value: unknown;
  source: string;
  confidence: number;
  status: string;
  evidence_ids: string[];
  supersedes_property_id: string | null;
  revision: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface EntityDetailFactPayload {
  id: string;
  scope_id: string;
  subject: string;
  predicate: string;
  object: string;
  status: string;
  confidence: number;
  evidence_ids: string[];
  revision: number;
}

export interface EntityDetailEventPayload {
  id: string;
  scope_id: string;
  title: string;
  event_type: string;
  time_start: string | null;
  time_end: string | null;
  place: string;
  admin_district: string;
  activity: string;
  summary: string;
  confidence: number;
  aggregation_score: number;
  status: string;
  revision: number;
}

export interface EntityDetailAppearancePayload {
  id: string;
  person_id: string;
  face_instance_id: string;
  observation_id: string;
  asset_id: string;
  crop_bbox: number[];
  clothing: string[];
  confidence: number;
  model_name: string;
  status: string;
  file_name: string;
  media_type: string;
}

export interface EntityEvidenceFilePayload {
  evidence_id: string;
  asset_id: string;
  file_name: string;
  kind: string;
}

export interface EntityDetailRelationshipPayload {
  id: string;
  scope_id: string;
  subject_entity_id: string;
  predicate: string;
  object_entity_id: string;
  inverse_predicate: string | null;
  status: string;
  confidence: number;
  evidence_ids: string[];
  revision: number;
  created_at: string | null;
  updated_at: string | null;
  subject_name: string;
  object_name: string;
}

/**
 * **完整**实体详情（`GET /entities/{id}` 的十个键）。
 *
 * 与人物证据页那份 `EntityDetailResponse`（只建模 entity/clusters/events/observations）
 * 分开命名：两份响应的投影不同，合成一个会让两边都出现恒空的键。
 */
export interface EntityDetailFullResponse {
  entity: EntityDetailEntityFull;
  properties: EntityPropertyPayload[];
  property_history: EntityPropertyPayload[];
  clusters: FaceClusterSummary[];
  facts: EntityDetailFactPayload[];
  events: EntityDetailEventPayload[];
  observations: ObservationPayload[];
  appearance_evidence: EntityDetailAppearancePayload[];
  evidence_files: EntityEvidenceFilePayload[];
  relationships: EntityDetailRelationshipPayload[];
}

/** 实体组的详情：组本身 + 成员详情 + 去重后的观察与事件。 */
export interface EntityGroupDetailResponse {
  group: EntityGroupPayload;
  members: EntityDetailFullResponse[];
  observations: ObservationPayload[];
  events: EntityDetailEventPayload[];
}

interface EntityGroupListResponse {
  groups: EntityGroupPayload[];
}

export async function listEntityGroups(scopeId: string): Promise<EntityGroupPayload[]> {
  const params = new URLSearchParams();
  if (scopeId) {
    params.set("scope_id", scopeId);
  }
  const suffix = params.toString();
  const body = await request<EntityGroupListResponse>(`/entity-groups${suffix ? `?${suffix}` : ""}`);
  return body.groups;
}

export async function getEntityGroup(
  groupId: string,
  scopeId: string,
): Promise<EntityGroupDetailResponse> {
  const params = new URLSearchParams();
  if (scopeId) {
    params.set("scope_id", scopeId);
  }
  const suffix = params.toString();
  return request<EntityGroupDetailResponse>(
    `/entity-groups/${encodeURIComponent(groupId)}${suffix ? `?${suffix}` : ""}`,
  );
}

export async function getEntity(entityId: string): Promise<EntityDetailFullResponse> {
  return request<EntityDetailFullResponse>(`/entities/${encodeURIComponent(entityId)}`);
}

/**
 * 用户写下一个实体属性。**这是「用户值优先」的那条路**：后端
 * `EntityRepository.set_property` 会顶掉派生值（`routes/identity.py` 的 docstring）。
 *
 * `property_key` 走路径，所以必须编码；`value` 可以是字符串、布尔或数组，
 * 前端不把它字符串化（后端按 JSON 原样落 `value_json`）。
 */
export async function updateEntityProperty(
  entityId: string,
  propertyKey: string,
  value: unknown,
  evidenceIds: string[] = [],
): Promise<EntityPropertyPayload> {
  return request<EntityPropertyPayload>(
    `/entities/${encodeURIComponent(entityId)}/properties/${encodeURIComponent(propertyKey)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value, evidence_ids: evidenceIds }),
    },
  );
}

// ---- 待确认事实（事实审核） -------------------------------------------------------
//
// 形状照 `apps/api/src/sentrix/api/schemas/facts.py`。**不做裁剪**：`status` 是页面
// 决定「要不要显示在待确认里」的唯一依据，少带它按钮就永远不会出现。
// 1.0 对应 `app.py:2539` 的 `GET /api/facts`、`:3905` confirm、`:3913` reject。

export interface FactPayload {
  id: string;
  scope_id: string;
  subject: string;
  predicate: string;
  object: string;
  /** `active` / `pending` / `superseded` / `retracted`。 */
  status: string;
  confidence: number;
  evidence_ids: string[];
  revision: number;
  supersedes_fact_id: string | null;
}

interface FactListResponse {
  facts: FactPayload[];
}

/**
 * 列出事实。`scopeId` 为空串表示**全部相册**——后端 `scope_id=""` 与不传同义
 * （`routes/facts.py` 的 docstring 写明这条沿用了 1.0）。
 */
export async function listFacts(
  scopeId = "",
  status = "",
  limit = 200,
): Promise<FactPayload[]> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (scopeId) params.set("scope_id", scopeId);
  if (status) params.set("status", status);
  const body = await request<FactListResponse>(`/facts?${params.toString()}`);
  return body.facts;
}

/** 确认一条事实：后端连旧版本的 superseded 一起做，前端不自己改状态。 */
export async function confirmFact(factId: string): Promise<FactPayload> {
  return request<FactPayload>(`/facts/${encodeURIComponent(factId)}/confirm`, { method: "POST" });
}

/** 驳回一条事实：原始证据保留，只改这一行的版本状态。 */
export async function rejectFact(factId: string): Promise<FactPayload> {
  return request<FactPayload>(`/facts/${encodeURIComponent(factId)}/reject`, { method: "POST" });
}

// ---- 视频处理（元数据 / 场景 / 重跑） ---------------------------------------------
//
// 形状照 `apps/api/src/sentrix/api/schemas/videos.py`，只带 2.0 前端真正消费的键：
// 视频详情给场景数，重跑给「已跑完」的回执。**没有视频状态机的中间态**——2.0 的
// `AssetStatus` 只有 queued / processing / processed / failed 四个值，1.0 的
// `video-metadata` / `video-keyframe-extracting` 之类在 2.0 不存在，前端不能伪造。

export interface VideoReprocessResponse {
  accepted: boolean;
  asset_id: string;
  status: string;
}

/**
 * 重跑一个视频的派生链。后端**同步**跑完（`routes/videos.py:185` 的 docstring），
 * 返回时资产已经是 `processed`；202 只是沿用 1.0 的状态码。
 */
export async function reprocessVideo(assetId: string): Promise<VideoReprocessResponse> {
  return request<VideoReprocessResponse>(
    `/videos/${encodeURIComponent(assetId)}/reprocess`,
    { method: "POST" },
  );
}
