import { useState } from "react";
import { apiJson } from "../api/client.js";
import { EXAMPLE_QUESTIONS, agentBody, proposalRequest } from "../lib/assist.js";
import { STORES } from "../lib/payment.js";

function Proposal({ proposal, onDone }) {
  const [state, setState] = useState({});

  async function approve() {
    if (!window.confirm(`${proposal.title}\n\nApprove this? PayRecover will act at PayPal and then confirm it.`)) return;
    setState({ busy: true });
    const { path, body } = proposalRequest(proposal);
    try {
      const result = await apiJson(path, { method: "POST", body }, "PayPal did not complete this step.");
      setState({ result });
      onDone?.(result);
    } catch (reason) {
      setState({ error: reason.message });
    }
  }

  return <div className="proposal">
    <span>{proposal.title}</span>
    {state.result
      ? <span className={`badge ${state.result.status === "failed" ? "failed" : "passed"}`} title={state.result.message}>{state.result.status.replaceAll("_", " ")}</span>
      : <button className="danger-button" type="button" disabled={state.busy} onClick={approve}>{state.busy ? "Asking PayPal…" : "Approve"}</button>}
    {state.error && <span className="badge failed" title={state.error}>Error</span>}
    {state.result && <small className="hint">{state.result.message}</small>}
  </div>;
}

function Message({ message, onDone }) {
  return <div className={`chat-message ${message.role}`}>
    <p>{message.text}</p>
    {message.tool_calls?.length > 0 && <details className="tool-calls">
      <summary>{message.tool_calls.length} tool call(s) · {message.source}</summary>
      <ul>{message.tool_calls.map((call, index) => <li key={index}>
        <span className={`badge ${call.ok ? "passed" : "failed"}`}>{call.source === "paypal_agent_toolkit" ? "PayPal Agent Toolkit" : "PayRecover"}</span>{" "}
        <code>{call.name}</code> {Object.keys(call.arguments).length > 0 && <code>{JSON.stringify(call.arguments)}</code>}
      </li>)}</ul>
    </details>}
    {message.proposals?.map((proposal, index) => <Proposal key={index} proposal={proposal} onDone={onDone} />)}
  </div>;
}

export default function AssistantPanel({ onRecovered }) {
  const [store, setStore] = useState("main");
  const [history, setHistory] = useState([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  async function ask(question) {
    const text = question.trim();
    if (!text || busy) return;
    const next = [...history, { role: "user", text }];
    setHistory(next);
    setDraft("");
    setBusy(true);
    setError(null);
    try {
      const reply = await apiJson("/api/agent", { method: "POST", body: agentBody(next, store) }, "The assistant could not answer.");
      setHistory([...next, { role: "assistant", ...reply }]);
    } catch (reason) {
      setError(reason.message);
    } finally { setBusy(false); }
  }

  return <section className="panel" id="assistant" aria-labelledby="assistant-heading">
    <div className="section-heading">
      <div><p className="eyebrow">PayRecover · Gemini + PayPal Agent Toolkit</p><h2 id="assistant-heading">Ask PayRecover</h2></div>
      <label className="field-label inline-select">Store
        <select className="text-input" value={store} onChange={(event) => setStore(event.target.value)}>
          {STORES.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
        </select>
      </label>
    </div>
    <p className="hint">Ask in plain words. The AI decides which PayPal and PayRecover tools to call, and shows every call. It can only propose moving money; nothing happens until you press Approve.</p>
    <div className="chat-log" aria-live="polite">
      {history.length === 0 && <div className="example-questions">{EXAMPLE_QUESTIONS.map((question) =>
        <button key={question} type="button" className="secondary-button" onClick={() => ask(question)}>{question}</button>)}</div>}
      {history.map((message, index) => <Message key={index} message={message} onDone={onRecovered} />)}
      {busy && <div className="chat-message assistant"><p><span className="spinner" aria-hidden="true" /> Checking PayPal…</p></div>}
    </div>
    {error && <p className="notice notice-error" role="alert">{error}</p>}
    <form className="chat-form" onSubmit={(event) => { event.preventDefault(); ask(draft); }}>
      <input className="text-input" value={draft} maxLength={4000} placeholder="e.g. Why did I lose money this week?"
        onChange={(event) => setDraft(event.target.value)} aria-label="Question for PayRecover" />
      <button className="danger-button" type="submit" disabled={busy || !draft.trim()}>Ask</button>
    </form>
  </section>;
}
