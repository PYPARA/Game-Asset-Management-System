import { useEffect, useState } from "react";
import { CheckCircle, Question, WarningCircle } from "@phosphor-icons/react";
import type { GenerationInputRequest } from "../types";

interface Props {
  request: GenerationInputRequest;
  onAnswer: (request: GenerationInputRequest, answers: Record<string, { answers: string[] }>, clientResponseId: string) => Promise<void>;
}

export function GenerationInputCard({ request, onAnswer }: Props) {
  const storageKey = `gams.input.${request.id}`;
  const saved = (() => {
    try { return JSON.parse(sessionStorage.getItem(storageKey) || "{}"); } catch { return {}; }
  })() as { values?: Record<string, string>; others?: Record<string, string>; responseId?: string };
  const [values, setValues] = useState<Record<string, string>>(saved.values ?? {});
  const [others, setOthers] = useState<Record<string, string>>(saved.others ?? {});
  const [responseId] = useState(() => saved.responseId ?? crypto.randomUUID());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (request.status !== "resolved") return;
    const persisted = Object.fromEntries(Object.entries(request.answers ?? {}).map(([id, value]) => [id, value.answers?.[0] ?? ""]));
    setValues((current) => ({ ...persisted, ...current }));
  }, [request.answers, request.status]);
  useEffect(() => {
    if (request.status === "pending") sessionStorage.setItem(storageKey, JSON.stringify({ values, others, responseId }));
    else sessionStorage.removeItem(storageKey);
  }, [others, request.status, responseId, storageKey, values]);

  const pending = request.status === "pending";
  const complete = request.questions.every((question) => {
    const value = values[question.id]?.trim();
    return value && (value !== "__other__" || others[question.id]?.trim());
  });
  const submit = async () => {
    if (!pending || !complete || busy) return;
    const answers = Object.fromEntries(request.questions.map((question) => [question.id, {
      answers: [values[question.id] === "__other__" ? others[question.id].trim() : values[question.id].trim()],
    }]));
    setBusy(true);
    setError("");
    try {
      await onAnswer(request, answers, responseId);
      sessionStorage.removeItem(storageKey);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "回答未提交，请重试。 ");
      // Preserve the response id on retries so a lost HTTP response remains idempotent.
    } finally {
      setBusy(false);
    }
  };

  return <section id={`generation-input-${request.id}`} className="generation-input-card" aria-label="Agent 需要你的回答" tabIndex={-1}>
    <header><Question size={17} /><strong>{pending ? "Agent 需要你的回答" : request.status === "resolved" ? "已回答" : "问题已取消"}</strong>
      {request.response_mode === "new_turn" && pending && <small>回答后开启新回合</small>}
    </header>
    {request.questions.map((question) => <fieldset key={question.id} disabled={!pending || busy}>
      <legend><span>{question.header}</span>{question.question}</legend>
      {question.options?.length ? <div className="generation-input-options">
        {question.options.map((option) => <label key={option.label}>
          <input type="radio" name={`${request.id}-${question.id}`} value={option.label} checked={values[question.id] === option.label} onChange={() => setValues((current) => ({ ...current, [question.id]: option.label }))} />
          <span><strong>{option.label}</strong><small>{option.description}</small></span>
        </label>)}
        {question.isOther && <label><input type="radio" name={`${request.id}-${question.id}`} checked={values[question.id] === "__other__"} onChange={() => setValues((current) => ({ ...current, [question.id]: "__other__" }))} /><span><strong>其他</strong></span></label>}
        {values[question.id] === "__other__" && <input aria-label={`${question.header}的其他回答`} maxLength={4000} value={others[question.id] ?? ""} onChange={(event) => setOthers((current) => ({ ...current, [question.id]: event.target.value }))} />}
      </div> : <textarea aria-label={question.header} maxLength={4000} rows={3} value={values[question.id] ?? ""} onChange={(event) => setValues((current) => ({ ...current, [question.id]: event.target.value }))} />}
      {request.status === "resolved" && <p>{request.answers[question.id]?.answers?.[0] ?? "已回答"}</p>}
    </fieldset>)}
    {error && <p className="generation-input-error" role="alert"><WarningCircle size={14} />{error}</p>}
    {pending && <footer><button className="button primary" type="button" onClick={() => void submit()} disabled={!complete || busy}>{busy ? "正在提交…" : request.response_mode === "new_turn" ? "提交并继续新回合" : "提交回答"}</button></footer>}
    {request.status === "resolved" && <p className="generation-input-resolved"><CheckCircle size={14} /> 已提交给 Agent</p>}
  </section>;
}
