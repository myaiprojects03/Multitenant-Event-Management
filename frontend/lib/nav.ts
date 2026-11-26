import {
  BarChart3,
  CalendarClock,
  ClipboardCheck,
  Gauge,
  MessageSquareText,
  Send,
  ShieldAlert,
  UserSearch,
  Users,
  Workflow,
  type LucideIcon,
} from "lucide-react";
import type { Role } from "./types";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  badge?: "approvals";
}

export interface NavGroup {
  label: string;
  items: NavItem[];
}

const assistant: NavItem = { href: "/assistant", label: "Event assistant", icon: MessageSquareText };
const security: NavItem = { href: "/security", label: "Security tests", icon: ShieldAlert };
const approvals: NavItem = { href: "/approvals", label: "Approvals", icon: ClipboardCheck, badge: "approvals" };

export const NAV: Record<Role, NavGroup[]> = {
  attendee: [
    { label: "Event", items: [assistant, { href: "/matches", label: "Recommendations", icon: Users }, { href: "/network", label: "Connections & meetings", icon: CalendarClock }] },
    { label: "Platform", items: [security] },
  ],
  exhibitor: [
    { label: "Booth", items: [{ href: "/prospects", label: "Prospects", icon: UserSearch }, { href: "/network", label: "Meetings & scans", icon: CalendarClock }, { href: "/follow-ups", label: "Lead follow-ups", icon: Send }, approvals] },
    { label: "Event", items: [assistant] },
    { label: "Platform", items: [security] },
  ],
  organizer: [
    { label: "Operations", items: [{ href: "/analytics", label: "BI copilot", icon: BarChart3 }, approvals, { href: "/workflows", label: "Workflows", icon: Workflow }] },
    { label: "Event", items: [assistant] },
    { label: "Platform", items: [{ href: "/telemetry", label: "Telemetry", icon: Gauge }, security] },
  ],
};

export const HOME: Record<Role, string> = {
  attendee: "/assistant",
  exhibitor: "/prospects",
  organizer: "/analytics",
};

export function allowed(role: Role, path: string): boolean {
  return NAV[role].some((g) => g.items.some((i) => path === i.href || path.startsWith(i.href + "/")));
}
