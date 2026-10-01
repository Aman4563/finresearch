"use client";

import { NotebookPen } from "lucide-react";
import { useState } from "react";

import { BehaviourReport } from "@/components/journal/behaviour-report";
import { IpoDecisions } from "@/components/journal/ipo-decisions";
import { PreTradePanel } from "@/components/journal/pretrade";
import { TradeJournal } from "@/components/journal/trade-journal";
import { PageHeader, Segmented } from "@/components/ui";

type Tab = "trades" | "ipo" | "behaviour";

export default function Journal() {
  const [tab, setTab] = useState<Tab>("trades");
  const [refresh, setRefresh] = useState(0);
  return (
    <div className="space-y-6">
      <PageHeader
        icon={<NotebookPen className="size-5" />}
        eyebrow="Workspace"
        title="Decision journal"
        description="Why you bought or sold, what would prove you wrong, and how it turned out: for every trade, IPO suggestions included. Your journal stays on this machine."
        actions={<Segmented<Tab> value={tab} onChange={setTab} options={[
          { value: "trades", label: "All decisions" },
          { value: "ipo", label: "IPO suggestions" },
          { value: "behaviour", label: "Behaviour report" },
        ]} />}
      />
      {tab === "trades" && (
        <div className="space-y-6">
          <PreTradePanel onRecorded={() => setRefresh((r) => r + 1)} />
          <TradeJournal refreshKey={refresh} />
        </div>
      )}
      {tab === "ipo" && <IpoDecisions />}
      {tab === "behaviour" && <BehaviourReport />}
    </div>
  );
}
