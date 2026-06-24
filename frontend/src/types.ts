export interface Agent {
  id: string;
  name: string;
  role: string;
  expertise: string;
  goal: string;
  tools: string[];
  model: string;
  status: string;
  icon: string;
}

export interface LabEvent {
  type: string;
  [key: string]: any;
}

export interface ActivityItem {
  id: number;
  kind: string;        // phase | agent | tool | critic | meeting | director | injection | system
  agent?: string;
  text: string;
  tone?: "ok" | "warn" | "bad" | "info";
  ts: number;
}

export interface Decision {
  action: string;       // continue | report | revise_team | more_rounds | report_fix
  reason: string;
  next_agenda: string;
  round: number;
  ts: number;
}
