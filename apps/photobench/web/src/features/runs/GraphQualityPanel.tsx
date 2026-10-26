import type {
  FaceClusteringMetrics,
  GraphQaByTypeRow,
  GraphQuality,
  GraphReferenceMetrics,
} from "../../api/types.ts";

function ratio(value: number | null | undefined): string {
  return typeof value === "number" ? `${(value * 100).toFixed(1)}%` : "—";
}

function number(value: number | null | undefined): string {
  return typeof value === "number" ? String(value) : "—";
}

function metricCard(label: string, value: string, note?: string) {
  return (
    <div className="phase-metric" key={label}>
      <span>{label}</span>
      <strong>{value}</strong>
      {note ? <small>{note}</small> : null}
    </div>
  );
}

function graphCards(graph: GraphReferenceMetrics) {
  return [
    metricCard("可验边 P", ratio(graph.reference_edge_precision)),
    metricCard("可验边 R", ratio(graph.reference_edge_recall)),
    metricCard("可验边 F1", ratio(graph.reference_edge_f1)),
    metricCard("有效边率", ratio(graph.valid_edge_rate)),
    metricCard("节点连通率", ratio(graph.node_connected_rate)),
    metricCard("边一致率", ratio(graph.edge_consistency_rate)),
    metricCard("重复边率", ratio(graph.duplicate_edge_rate)),
    metricCard("证据支持边率", ratio(graph.edge_evidence_support_rate)),
    metricCard("不可验边占比", ratio(graph.unverifiable_edge_ratio)),
    metricCard("参考 / 预测可验边", `${number(graph.reference_edge_count)} / ${number(graph.evaluable_predicted_edge_count)}`),
    metricCard("节点 / 边", `${number(graph.node_count)} / ${number(graph.edge_count)}`),
  ];
}

function faceCards(face: FaceClusteringMetrics) {
  const gallery = face.identity_gallery || {};
  const links = face.image_identity_links || {};
  return [
    metricCard("身份 Gallery F1", ratio(gallery.f1)),
    metricCard("身份 Gallery P", ratio(gallery.precision)),
    metricCard("身份 Gallery R", ratio(gallery.recall)),
    metricCard("图像身份 Link F1", ratio(links.f1)),
    metricCard("单脸检测率", ratio(face.single_face_detection_rate)),
    metricCard("Gallery 种子数", number(face.identity_gallery_seed_count)),
    metricCard("GT 图像数", number(gallery.gt_asset_count)),
    metricCard("已识别图像数", number(gallery.identified_image_count)),
  ];
}

function qaCell(value: number | null | undefined, percent = true): string {
  if (typeof value !== "number") return "—";
  return percent ? ratio(value) : value.toFixed(3);
}

function QuestionTypeTable({ rows }: { rows: GraphQaByTypeRow[] }) {
  if (!rows.length) return <p className="metric-calc-time">没有可汇总的题型数据。</p>;
  return (
    <div className="call-table-wrap graph-quality-table-wrap">
      <table className="call-table graph-quality-table">
        <thead>
          <tr>
            <th>问题类型</th>
            <th>题数</th>
            <th>图路由</th>
            <th>图生效</th>
            <th>图召回 Δ</th>
            <th>检索 R</th>
            <th>回答质量</th>
            <th>证据支持率</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.type || row.label}>
              <td>{row.label || row.type || "—"}</td>
              <td>{number(row.sample_count)}</td>
              <td>{number(row.graph_routed_count)}</td>
              <td>{number(row.graph_effect_count)}</td>
              <td>{row.graph_recall_delta == null ? "—" : `${row.graph_recall_delta >= 0 ? "+" : ""}${(row.graph_recall_delta * 100).toFixed(1)}%`}</td>
              <td>{qaCell(row.retrieval_recall)}</td>
              <td>{row.answer_quality_mean == null ? "—" : row.answer_quality_mean.toFixed(2)}</td>
              <td>{qaCell(row.evidence_supported_rate)}</td>
              <td>{row.telemetry_status === "recorded" ? "已记录" : "历史 run 未记录图遥测"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function GraphQualityPanel({ quality }: { quality: GraphQuality | null }) {
  if (!quality) {
    return (
      <section className="phase-card result-phase-card graph-quality-panel">
        <h3 className="phase-title">图记忆质量</h3>
        <p className="metric-calc-time">图质量指标读取中…</p>
      </section>
    );
  }
  if (quality.status === "unavailable") {
    return (
      <section className="phase-card result-phase-card graph-quality-panel">
        <h3 className="phase-title">图记忆质量</h3>
        <p className="metric-calc-time">暂不可用：{String(quality.reason || "读取失败")}</p>
      </section>
    );
  }
  const graph = quality.graph || {};
  const face = quality.face_clustering || {};
  return (
    <section className="phase-card result-phase-card graph-quality-panel">
      <h3 className="phase-title"><b>图记忆质量</b></h3>
      <p className="metric-calc-time">
        {graph.reference_mode === "source_consistency"
          ? "可验边参考集从 2.0 记忆来源字段重建，不读取图中已生成的边。无独立人工边 GT，P/R/F1 仅表示构图投影一致性；视觉与因果推断边不计入。"
          : "可验边采用规则参考集；无独立人工边 GT，P/R/F1 不是现实世界准确率。"}
      </p>
      {graph.available ? <div className="phase-metrics graph-quality-metrics">{graphCards(graph)}</div> : <p className="metric-calc-time">图结构：{String(graph.reason || "未构建")}</p>}

      <h4>人脸身份聚类质量</h4>
      {face.available ? <div className="phase-metrics graph-quality-metrics">{faceCards(face)}</div> : <p className="metric-calc-time">{String(face.reason || "未找到 identity GT sidecar")}</p>}

      <h4>按问题类型的图记忆 QA 表现</h4>
      <QuestionTypeTable rows={quality.graph_qa_by_type || []} />
    </section>
  );
}
