"use client";

import { PageHeader, SkeletonRows } from "@/components/ui";

export default function Page() {
  return (
    <div>
      <PageHeader title="Rules" description="Coming in this release." />
      <SkeletonRows rows={4} />
    </div>
  );
}
