import type { AssetKind } from "../types";

const kindLabels: Record<AssetKind, string> = {
  content: "叙事内容",
  design: "设计文档",
  entity: "游戏实体",
  media: "2D 媒体",
  production: "生产资料",
};

const subtypeLabels: Record<string, string> = {
  story_arc: "故事弧",
  event: "事件",
  memorial: "奏折",
  character: "角色",
  item: "物品",
  achievement: "成就",
  portrait: "角色立绘",
  background: "场景背景",
  cg: "剧情 CG",
  icon: "物品图标",
  visual_anchor: "视觉锚点",
  style_bible: "风格圣经",
  audio: "音频",
};

const domainLabels: Record<string, string> = {
  arcana: "方术",
  court: "朝堂",
  diplomacy: "外交",
  governance: "政务",
  harem: "后宫",
  main: "主线",
  npc: "人物",
  region: "地方",
  regions: "地方",
  war: "军事",
  works: "营造",
  trade: "商贸",
};

export function assetKindLabel(kind: AssetKind): string {
  return kindLabels[kind];
}

export function assetSubtypeLabel(kind: AssetKind, subtype: string): string {
  if (subtype === "ending") return kind === "media" ? "结局图" : "结局内容";
  return subtypeLabels[subtype] ?? "自定义类型";
}

export function assetSubtypeTitle(kind: AssetKind, subtype: string): string {
  const label = assetSubtypeLabel(kind, subtype);
  return label === "自定义类型" ? `${label}：${subtype}` : `${label}（${subtype}）`;
}

export function domainLabel(domain: string): string {
  return domainLabels[domain.toLocaleLowerCase()] ?? domain;
}

export function fieldLabel(field: string): string {
  const labels: Record<string, string> = {
    id: "原始 ID",
    title: "标题",
    name: "名称",
    synopsis: "梗概",
    summary: "摘要",
    description: "描述",
    text: "正文",
    act: "幕",
    domain: "领域",
    participants: "参与角色",
    nodes: "包含事件",
    references: "引用资产",
    source: "来源",
    choices: "选项",
    effects: "效果",
    condition: "条件",
    notes: "备注",
    points: "点数",
    priority: "优先级",
    faction: "阵营",
    gender: "性别",
    age: "年龄",
    tags: "标签",
  };
  return labels[field] ?? field;
}
