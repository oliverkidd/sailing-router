"use client";

import PlannerWorkspace from "@/components/shell/PlannerWorkspace";

/**
 * Keep one planner session + map across / and /route so mode switches do not
 * remount MapView or force a camera jump.
 */
export default function PlannerLayout({ children }: { children: React.ReactNode }) {
  return (
    <>
      <PlannerWorkspace />
      {children}
    </>
  );
}
