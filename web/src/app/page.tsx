"use client";

import { LayoutDashboard } from "lucide-react";

import { PageHeader, SkeletonRows } from "@/components/ui";

// placeholder until the dashboard lands (built on the shared components)
export default function Dashboard() {
  return (
    <div>
      <PageHeader icon={<LayoutDashboard className="size-5" />} title="Dashboard" description="Everything at a glance." />
      <SkeletonRows rows={6} />
    </div>
  );
}
