"use client";

import dynamic from "next/dynamic";

const CoverageMap = dynamic(() => import("@/components/CoverageMap"), { ssr: false });

export default function CoveragePage() {
  return <CoverageMap />;
}
