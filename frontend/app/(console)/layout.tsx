"use client";

import { usePathname, useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";
import { Shell } from "@/components/Shell";
import { allowed, HOME } from "@/lib/nav";
import { useSession } from "@/lib/session";

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const { session, ready } = useSession();
  const router = useRouter();
  const path = usePathname();
  const ok = session && allowed(session.role, path);

  useEffect(() => {
    if (!ready) return;
    if (!session) router.replace("/");
    else if (!allowed(session.role, path)) router.replace(HOME[session.role]);
  }, [ready, session, path, router]);

  if (!ready || !ok) return null;
  // key: a different user or event remounts every page, so no data leaks across sessions in state
  return (
    <Shell key={`${session.user_id}:${session.event_id}`}>
      {children}
    </Shell>
  );
}
