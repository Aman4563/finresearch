"use client";

import { CheckCircle2, CircleHelp, RotateCcw, XCircle } from "lucide-react";
import { useState } from "react";

import { Button, Card, cx } from "@/components/ui";

import type { Question } from "./types";

/** A self-check: answers stay in this tab only (nothing is stored or sent). */
export function Quiz({ questions }: { questions: Question[] }) {
  const [picked, setPicked] = useState<(number | null)[]>(() => questions.map(() => null));
  const done = picked.filter((p) => p != null).length;
  const right = picked.filter((p, i) => p === questions[i].answer).length;
  return (
    <Card title="Self-check" icon={<CircleHelp className="size-4" />}
      subtitle="Answers stay in this browser tab; nothing is saved or sent."
      actions={done > 0 && (
        <Button variant="ghost" icon={<RotateCcw className="size-3.5" />} onClick={() => setPicked(questions.map(() => null))}>Reset</Button>
      )}>
      <ol className="space-y-5">
        {questions.map((q, i) => {
          const p = picked[i];
          return (
            <li key={q.q} className="space-y-2">
              <p className="text-sm font-medium">{i + 1}. {q.q}</p>
              <div role="radiogroup" aria-label={`Question ${i + 1}`} className="grid gap-1.5">
                {q.options.map((o, j) => {
                  const chosen = p === j, correct = j === q.answer;
                  return (
                    <button key={o} type="button" role="radio" aria-checked={chosen} disabled={p != null}
                      onClick={() => setPicked((prev) => prev.map((x, k) => (k === i ? j : x)))}
                      className={cx("flex items-start gap-2 rounded-lg border px-3 py-2 text-left text-sm transition",
                        p == null && "border-border hover:border-border-strong hover:bg-card-hover",
                        p != null && correct && "border-gain bg-gain-soft",
                        p != null && chosen && !correct && "border-loss bg-loss-soft",
                        p != null && !chosen && !correct && "border-border opacity-70")}>
                      {p != null && correct && <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-gain" aria-label="correct answer" />}
                      {p != null && chosen && !correct && <XCircle className="mt-0.5 size-4 shrink-0 text-loss" aria-label="your answer, incorrect" />}
                      <span>{o}</span>
                    </button>
                  );
                })}
              </div>
              {p != null && (
                <p className="rounded-lg bg-background-subtle px-3 py-2 text-xs leading-relaxed text-muted" role="status">
                  <span className={cx("font-semibold", p === q.answer ? "text-gain" : "text-loss")}>{p === q.answer ? "Correct. " : "Not quite. "}</span>
                  {q.why}
                </p>
              )}
            </li>
          );
        })}
      </ol>
      {done === questions.length && (
        <p className="mt-4 text-sm font-medium" role="status">Score: <span className="num">{right} / {questions.length}</span>. Re-read the sections behind any miss before moving on.</p>
      )}
    </Card>
  );
}
