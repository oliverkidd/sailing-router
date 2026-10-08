"use client";

/**
 * Planner session lives in PlannerWorkspace (shared across / and /route via
 * component state). This module is the stable import surface for a future
 * layout-level provider if MapView is lifted out of the page remount cycle.
 */
export { default as PlannerWorkspace } from "@/components/shell/PlannerWorkspace";
