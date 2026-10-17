import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api } from '@/api/client';
import { PageHeader, useApp } from '@/components/Shell';
import { Empty, ErrorState, Icon, Modal, ScopeLabel, SelectMenu, Seg, Sk, SkLines, Standby, useToast } from '@/components/ui';
import { useApi } from '@/hooks/useApi';
import { buildHash, navigate, type HashRoute } from '@/hooks/useHashRoute';
import type { GraphNeighbourhood, NodeBrief } from '@/types';
import { fmtWeight, scopeMeta, shortId, truncate } from '@/utils/format';
import { MemoryPicker, RelationshipModal } from './common';

const W = 900;
const H = 680;
const EDGE_TYPES = ['related_to', 'derived_from', 'supersedes', 'conflicts_with'] as const;
const EDGE_STYLE: Record<string, { color: string; dash?: string; marker: string }> = {
  derived_from: { color: '#3d7317', marker: 'arg' },
  supersedes: { color: '#5e40be', marker: 'arp' },
  conflicts_with: { color: '#b1380b', dash: '6 5', marker: 'arr' },
  related_to: { color: '#8a8d90', marker: 'ar' },
};
const edgeStyle = (t: string) => EDGE_STYLE[t] ?? EDGE_STYLE.related_to;

type Layout = 'force' | 'tree' | 'timeline';
type Pos = Record<string, { x: number; y: number }>;

/** Deterministic force layout: focus pinned in the centre. */
function forceLayout(nodes: GraphNeighbourhood['nodes'], edges: GraphNeighbourhood['edges'], focusId: string): Pos {
  const pos: Pos = {};
  const byDepth: Record<number, string[]> = {};
  nodes.forEach((n) => (byDepth[n.depth] = [...(byDepth[n.depth] ?? []), n.id]));
  Object.entries(byDepth).forEach(([d, ids]) => {
    const r = Number(d) * 190;
    ids.forEach((id, i) => {
      const a = (i / ids.length) * Math.PI * 2 + Number(d) * 0.7;
      pos[id] = { x: W / 2 + Math.cos(a) * r, y: H / 2 + Math.sin(a) * r * 0.8 };
    });
  });
  const ids = nodes.map((n) => n.id);
  for (let iter = 0; iter < 260; iter += 1) {
    const force: Record<string, { x: number; y: number }> = {};
    ids.forEach((id) => (force[id] = { x: 0, y: 0 }));
    for (let i = 0; i < ids.length; i += 1)
      for (let j = i + 1; j < ids.length; j += 1) {
        const a = pos[ids[i]];
        const b = pos[ids[j]];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) {
          dx = Math.random();
          dy = Math.random();
          d2 = 1;
        }
        const f = 40000 / d2;
        const d = Math.sqrt(d2);
        force[ids[i]].x += (dx / d) * f;
        force[ids[i]].y += (dy / d) * f;
        force[ids[j]].x -= (dx / d) * f;
        force[ids[j]].y -= (dy / d) * f;
      }
    edges.forEach((e) => {
      const a = pos[e.source];
      const b = pos[e.target];
      if (!a || !b) return;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - 230) * 0.04;
      force[e.source].x += (dx / d) * f;
      force[e.source].y += (dy / d) * f;
      force[e.target].x -= (dx / d) * f;
      force[e.target].y -= (dy / d) * f;
    });
    const cool = 1 - iter / 260;
    ids.forEach((id) => {
      if (id === focusId) return;
      const p = pos[id];
      p.x += Math.max(-30, Math.min(30, force[id].x + (W / 2 - p.x) * 0.004)) * cool;
      p.y += Math.max(-30, Math.min(30, force[id].y + (H / 2 - p.y) * 0.004)) * cool;
      p.x = Math.max(60, Math.min(W - 60, p.x));
      p.y = Math.max(60, Math.min(H - 60, p.y));
    });
    pos[focusId] = { x: W / 2, y: H / 2 };
  }
  return pos;
}

function treeLayout(nodes: GraphNeighbourhood['nodes']): Pos {
  const pos: Pos = {};
  const byDepth: Record<number, typeof nodes> = {};
  nodes.forEach((n) => (byDepth[n.depth] = [...(byDepth[n.depth] ?? []), n]));
  const depths = Object.keys(byDepth).map(Number).sort();
  depths.forEach((d) => {
    const row = byDepth[d];
    row.forEach((n, i) => {
      pos[n.id] = { x: ((i + 1) / (row.length + 1)) * W, y: 90 + (d / Math.max(1, depths.length - 1 || 1)) * (H - 180) };
    });
  });
  return pos;
}

function timelineLayout(nodes: GraphNeighbourhood['nodes']): Pos {
  const pos: Pos = {};
  const times = nodes.map((n) => new Date(n.created_at).getTime());
  const min = Math.min(...times);
  const max = Math.max(...times);
  const lanes = ['user', 'project', 'campaign', 'role', 'organizational', 'enterprise'];
  nodes.forEach((n, i) => {
    const t = times[i];
    const lane = Math.max(0, lanes.indexOf(n.scope));
    pos[n.id] = { x: 80 + (max === min ? 0.5 : (t - min) / (max - min)) * (W - 160), y: 90 + lane * ((H - 180) / 5) };
  });
  return pos;
}

const Graph: React.FC<{ route: HashRoute }> = ({ route }) => {
  const toast = useToast();
  const { standby } = useApp();
  const focus = route.params.get('focus') || undefined;
  const depth = Number(route.params.get('depth') || 2);
  const [layout, setLayout] = useState<Layout>('force');
  const [types, setTypes] = useState<Set<string>>(new Set(EDGE_TYPES));
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const [picker, setPicker] = useState(false);
  const [relate, setRelate] = useState(false);
  const svgRef = useRef<SVGSVGElement>(null);
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);

  const { data, error, loading, reload } = useApi<GraphNeighbourhood>(() => api.neighbourhood({ focus, depth }), [focus, depth], standby ? 5000 : null);

  useEffect(() => {
    setSelected(data?.focus?.id ?? null);
    setView({ x: 0, y: 0, k: 1 });
  }, [data?.focus?.id]);

  const setParam = (k: string, v: string | undefined) => {
    const next = new URLSearchParams(route.params);
    if (v) next.set(k, v);
    else next.delete(k);
    navigate('/graph', next, { replace: k !== 'focus' });
  };

  const visibleEdges = useMemo(
    () => (data?.edges ?? []).filter((e) => types.has(e.type) || !EDGE_TYPES.includes(e.type as never)),
    [data, types],
  );
  const positions = useMemo<Pos>(() => {
    if (!data?.focus) return {};
    if (layout === 'tree') return treeLayout(data.nodes);
    if (layout === 'timeline') return timelineLayout(data.nodes);
    return forceLayout(data.nodes, data.edges, data.focus.id);
  }, [data, layout]);

  const nodeById = useMemo(() => Object.fromEntries((data?.nodes ?? []).map((n) => [n.id, n])), [data]);
  const sel = selected ? nodeById[selected] : undefined;
  const focusNode = data?.focus;
  const isFocusSel = sel && focusNode && sel.id === focusNode.id;
  const selDegree = sel ? (data?.edges ?? []).filter((e) => e.source === sel.id || e.target === sel.id).length : 0;

  const exportPng = () => {
    const svgEl = svgRef.current;
    if (!svgEl) return;
    const xml = new XMLSerializer().serializeToString(svgEl);
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement('canvas');
      canvas.width = W * 2;
      canvas.height = H * 2;
      const ctx = canvas.getContext('2d');
      if (!ctx) return;
      ctx.fillStyle = '#fafafa';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      const a = document.createElement('a');
      a.download = `memory-graph-${focusNode ? shortId(focusNode.id) : 'tenant'}.png`;
      a.href = canvas.toDataURL('image/png');
      a.click();
    };
    img.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(xml)}`;
  };

  const onWheel = (e: React.WheelEvent) => {
    const k = Math.max(0.4, Math.min(3, view.k * (e.deltaY < 0 ? 1.1 : 0.9)));
    setView((v) => ({ ...v, k }));
  };

  const hiddenPos = focusNode && positions[focusNode.id] ? { x: Math.max(70, positions[focusNode.id].x - 330), y: Math.max(70, positions[focusNode.id].y - 220) } : null;

  return (
    <>
      <PageHeader
        title="Memory graph"
        description="Explore how memories connect: provenance, supersession and conflicts. Nodes you cannot read are counted, never shown."
        actions={
          <>
            <button type="button" className="btn bg" disabled={!focusNode} onClick={exportPng}>
              Export PNG
            </button>
            <button type="button" className="btn bs" disabled={!sel} onClick={() => setRelate(true)}>
              Create relationship
            </button>
          </>
        }
      />
      <div className="body tight">
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'center' }}>
          <button type="button" className="sel" disabled={standby} onClick={() => setPicker(true)}>
            Focus: {focusNode ? <span className="mono">{shortId(focusNode.id)}</span> : 'most connected'} <Icon.Chevron />
          </button>
          <Seg
            label="Layout"
            value={layout}
            onChange={setLayout}
            disabled={standby}
            options={[
              { value: 'force', label: 'Force' },
              { value: 'tree', label: 'Provenance tree' },
              { value: 'timeline', label: 'Timeline' },
            ]}
          />
          <SelectMenu
            label="Depth"
            prefix="Depth: "
            value={String(depth)}
            onChange={(v) => setParam('depth', v === '2' ? undefined : v)}
            options={[1, 2, 3].map((d) => ({ value: String(d), label: `${d} hop${d === 1 ? '' : 's'}` }))}
          />
          {EDGE_TYPES.map((t) => (
            <label className="tg" key={t}>
              <input
                type="checkbox"
                checked={types.has(t)}
                aria-label={t}
                onChange={() => {
                  const next = new Set(types);
                  if (next.has(t)) next.delete(t);
                  else next.add(t);
                  setTypes(next);
                }}
              />{' '}
              {t}
            </label>
          ))}
          <span className="sm muted" style={{ marginLeft: 'auto' }}>
            {data ? `${data.nodes.length} nodes · ${visibleEdges.length} edges · ${data.hidden_count} hidden` : ''}
          </span>
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 16, alignItems: 'stretch' }}>
          <div className="card graph-wrap">
            {standby ? (
              <div style={{ display: 'grid', placeItems: 'center', minHeight: 680 }}>
                <Standby compact={false} title="Waiting for the first memory">
                  The graph draws memories as nodes, coloured by scope, and their provenance, supersession and conflict links as edges.
                </Standby>
              </div>
            ) : error && !data ? (
              <div style={{ display: 'grid', placeItems: 'center', minHeight: 680 }}>
                <ErrorState error={error} onRetry={reload} />
              </div>
            ) : loading && !data ? (
              <Sk h={680} r={0} />
            ) : !focusNode ? (
              <div style={{ display: 'grid', placeItems: 'center', minHeight: 680 }}>
                <Empty title="No memories to draw">Memories and their relationships appear here once agents write them.</Empty>
              </div>
            ) : (
              <>
                <svg
                  ref={svgRef}
                  viewBox={`${W / 2 - W / 2 / view.k - view.x} ${H / 2 - H / 2 / view.k - view.y} ${W / view.k} ${H / view.k}`}
                  role="img"
                  aria-label={`Graph centred on “${focusNode.stub}”, ${data!.nodes.length} memories and ${visibleEdges.length} relationships`}
                  onWheel={onWheel}
                  onPointerDown={(e) => {
                    if ((e.target as Element).closest('g.node')) return;
                    drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y };
                  }}
                  onPointerMove={(e) => {
                    if (!drag.current || !svgRef.current) return;
                    const scale = W / view.k / svgRef.current.clientWidth;
                    setView((v) => ({ ...v, x: drag.current!.vx + (e.clientX - drag.current!.x) * scale, y: drag.current!.vy + (e.clientY - drag.current!.y) * scale }));
                  }}
                  onPointerUp={() => (drag.current = null)}
                  onPointerLeave={() => (drag.current = null)}
                >
                  <defs>
                    <pattern id="dots" width="24" height="24" patternUnits="userSpaceOnUse">
                      <circle cx="2" cy="2" r="1" fill="#d9d9d9" />
                    </pattern>
                    {Object.entries({ ar: '#8a8d90', arg: '#3d7317', arr: '#b1380b', arp: '#5e40be' }).map(([id, fill]) => (
                      <marker key={id} id={id} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto">
                        <path d="M0 0L10 5L0 10z" fill={fill} />
                      </marker>
                    ))}
                  </defs>
                  <rect x={-W} y={-H} width={W * 3} height={H * 3} fill="url(#dots)" />
                  <g fill="none" strokeWidth="2">
                    {visibleEdges.map((e) => {
                      const a = positions[e.source];
                      const b = positions[e.target];
                      if (!a || !b) return null;
                      const st = edgeStyle(e.type);
                      const touchesFocus = e.source === focusNode.id || e.target === focusNode.id;
                      const rTarget = e.target === focusNode.id ? 32 : 6 + (nodeById[e.target]?.weight ?? 0.5) * 12;
                      const dx = b.x - a.x;
                      const dy = b.y - a.y;
                      const d = Math.sqrt(dx * dx + dy * dy) || 1;
                      return (
                        <line
                          key={e.id}
                          x1={a.x}
                          y1={a.y}
                          x2={b.x - (dx / d) * rTarget}
                          y2={b.y - (dy / d) * rTarget}
                          stroke={st.color}
                          strokeDasharray={st.dash}
                          strokeOpacity={touchesFocus ? 1 : 0.6}
                          markerEnd={touchesFocus ? `url(#${st.marker})` : undefined}
                        >
                          <title>{e.type}</title>
                        </line>
                      );
                    })}
                  </g>
                  <g fontFamily="Red Hat Text" fontSize="12" fill="#151515">
                    {data!.nodes.map((n) => {
                      const p = positions[n.id];
                      if (!p) return null;
                      const isFocus = n.id === focusNode.id;
                      const r = isFocus ? 30 : n.depth === 1 ? 14 + n.weight * 6 : 9 + n.weight * 3;
                      const color = scopeMeta(n.scope).color;
                      const labelled = isFocus || n.depth === 1 || data!.nodes.length <= 14;
                      return (
                        <g
                          key={n.id}
                          className="node"
                          tabIndex={0}
                          role="button"
                          aria-label={`${scopeMeta(n.scope).label} memory: ${n.stub}`}
                          onClick={() => setSelected(n.id)}
                          onDoubleClick={() => setParam('focus', n.id)}
                          onKeyDown={(e) => e.key === 'Enter' && setSelected(n.id)}
                        >
                          <title>{n.stub}</title>
                          <circle cx={p.x} cy={p.y} r={r} fill={color} fillOpacity={n.is_current && n.status === 'active' ? 1 : 0.4} stroke="#fff" strokeWidth={isFocus ? 4 : 3} />
                          {(isFocus || selected === n.id) && (
                            <circle cx={p.x} cy={p.y} r={r + 8} fill="none" stroke={color} strokeWidth="2" strokeDasharray="3 4" />
                          )}
                          {labelled && (
                            <text x={p.x} y={p.y + r + 17} textAnchor="middle" fontWeight={isFocus ? 600 : 400} fill={n.depth > 1 ? '#4d4d4d' : '#151515'}>
                              {truncate(n.stub, isFocus ? 34 : 26)}
                              {!n.is_current ? ` (v${n.version})` : ''}
                            </text>
                          )}
                        </g>
                      );
                    })}
                    {data!.hidden_count > 0 && hiddenPos && (
                      <g>
                        <circle cx={hiddenPos.x} cy={hiddenPos.y} r="12" fill="none" stroke="#8a8d90" strokeWidth="2" strokeDasharray="3 3" />
                        <text x={hiddenPos.x} y={hiddenPos.y + 28} textAnchor="middle" fill="#4d4d4d">
                          +{data!.hidden_count} hidden
                        </text>
                      </g>
                    )}
                  </g>
                </svg>
                <div className="legend">
                  <b style={{ fontSize: 12 }}>Scope</b>
                  {[...new Set(data!.nodes.map((n) => n.scope))].map((s) => (
                    <span key={s}>
                      <span style={{ width: 10, height: 10, borderRadius: 99, background: scopeMeta(s).color }} />
                      {scopeMeta(s).label}
                    </span>
                  ))}
                  <b style={{ fontSize: 12, marginTop: 6 }}>Edge</b>
                  {EDGE_TYPES.map((t) => (
                    <span key={t}>
                      <span style={{ width: 18, borderTop: `2px ${edgeStyle(t).dash ? 'dashed' : 'solid'} ${edgeStyle(t).color}` }} />
                      {t}
                    </span>
                  ))}
                </div>
                <div className="zoom">
                  <button type="button" aria-label="Zoom in" onClick={() => setView((v) => ({ ...v, k: Math.min(3, v.k * 1.25) }))}>
                    +
                  </button>
                  <button type="button" aria-label="Zoom out" onClick={() => setView((v) => ({ ...v, k: Math.max(0.4, v.k / 1.25) }))}>
                    −
                  </button>
                  <button type="button" aria-label="Fit to screen" onClick={() => setView({ x: 0, y: 0, k: 1 })}>
                    Fit
                  </button>
                </div>
              </>
            )}
          </div>

          <aside className="card" style={{ flex: '1 1 320px', display: 'flex', flexDirection: 'column', gap: 16 }}>
            {standby ? (
              <Standby>Select a node to see its owner, weight, version, provenance chain and conflicts.</Standby>
            ) : loading && !data ? (
              <SkLines lines={9} />
            ) : !sel ? (
              <Empty compact title="Nothing selected">
                Click a node to inspect it. Double-click to re-centre the graph on it.
              </Empty>
            ) : (
              <SidePanel
                node={sel}
                focus={Boolean(isFocusSel)}
                data={data!}
                degree={selDegree}
                onClose={() => setSelected(null)}
                onExpand={() => setParam('focus', sel.id)}
              />
            )}
          </aside>
        </div>
      </div>

      {picker && (
        <Modal title="Focus memory" onClose={() => setPicker(false)}>
          <MemoryPicker
            autoFocus
            onPick={(m) => {
              setPicker(false);
              setParam('focus', m.id);
            }}
          />
        </Modal>
      )}
      {relate && sel && (
        <RelationshipModal
          sourceIds={[sel.id]}
          onClose={() => setRelate(false)}
          onDone={() => {
            toast('Graph updated.');
            reload();
          }}
        />
      )}
    </>
  );
};

const SidePanel: React.FC<{
  node: NodeBrief & { depth: number };
  focus: boolean;
  data: GraphNeighbourhood;
  degree: number;
  onClose: () => void;
  onExpand: () => void;
}> = ({ node, focus, data, degree, onClose, onExpand }) => {
  const conflicts = focus
    ? data.conflicts
    : data.edges
        .filter((e) => e.type === 'conflicts_with' && (e.source === node.id || e.target === node.id))
        .map((e) => data.nodes.find((n) => n.id === (e.source === node.id ? e.target : e.source)))
        .filter(Boolean) as NodeBrief[];
  const content = focus && data.focus ? data.focus.content : node.stub;
  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <ScopeLabel scope={node.scope}>{scopeMeta(node.scope).label} · selected</ScopeLabel>
        <button type="button" className="btn bl" aria-label="Close panel" onClick={onClose}>
          ✕
        </button>
      </div>
      <h3 style={{ fontSize: 18, lineHeight: 1.35 }}>{content}</h3>
      <dl className="dl">
        <dt>Owner</dt>
        <dd>{node.scope_id ?? node.owner_id}</dd>
        <dt>Weight</dt>
        <dd>{fmtWeight(node.weight)}</dd>
        <dt>Version</dt>
        <dd>
          v{node.version} · {node.is_current ? 'current' : 'superseded'}
          {node.status !== 'active' ? ` · ${node.status}` : ''}
        </dd>
        <dt>Degree</dt>
        <dd>
          {degree} visible{focus && data.focus ? ` · ${data.focus.hidden_degree} hidden` : ''}
        </dd>
      </dl>
      {focus && data.provenance.length > 1 && (
        <div>
          <div className="sm" style={{ fontWeight: 600, marginBottom: 8 }}>
            Provenance chain
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, fontSize: 13 }}>
            {data.provenance.map((p, i) => (
              <React.Fragment key={p.id}>
                {i > 0 && (
                  <div className="muted" style={{ paddingLeft: 18 }}>
                    ↓ derived_from
                  </div>
                )}
                <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                  <ScopeLabel scope={p.scope}>{p.scope === 'organizational' ? 'org' : p.scope}</ScopeLabel>
                  {p.id === node.id ? 'This memory' : truncate(p.stub, 40)}
                </div>
              </React.Fragment>
            ))}
          </div>
        </div>
      )}
      {conflicts.length > 0 && (
        <div className="conflict">
          <b>Conflict detected</b>
          <br />
          <span className="muted">
            conflicts_with {conflicts.map((c) => `“${truncate(c.stub, 60)}” (${c.scope} ${c.scope_id ?? c.owner_id})`).join(', ')}.
          </span>
          <div style={{ marginTop: 8 }}>
            <a href={buildHash('/review', { tab: 'contradictions' })}>Resolve in review queue</a>
          </div>
        </div>
      )}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginTop: 'auto' }}>
        <a className="btn bp" href={buildHash(`/memory/${node.id}`)}>
          Open memory
        </a>
        {!focus && (
          <button type="button" className="btn bg" onClick={onExpand}>
            Expand neighbours
          </button>
        )}
      </div>
    </>
  );
};

export default Graph;
