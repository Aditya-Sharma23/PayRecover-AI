import { ASSERTION_OPERATORS, FLOW_ACTIONS, emptyAssertion, emptyHeader, emptyStep, flowAction } from "../lib/requestBody.js";

const MAX_STEPS = 10;
const MAX_ASSERTIONS = 20;
const MAX_HEADERS = 10;

function updateAt(rows, index, change) {
  return rows.map((row, position) => (position === index ? { ...row, ...change } : row));
}

function RowButtons({ index, count, onMove, onRemove }) {
  return <span className="row-buttons">
    {onMove && <button type="button" className="icon-button" aria-label={`Move row ${index + 1} up`} disabled={index === 0} onClick={() => onMove(index, -1)}>↑</button>}
    {onMove && <button type="button" className="icon-button" aria-label={`Move row ${index + 1} down`} disabled={index === count - 1} onClick={() => onMove(index, 1)}>↓</button>}
    <button type="button" className="icon-button" aria-label={`Remove row ${index + 1}`} onClick={() => onRemove(index)}>×</button>
  </span>;
}

export function FlowEditor({ steps, onChange }) {
  const move = (index, delta) => {
    const next = [...steps];
    [next[index], next[index + delta]] = [next[index + delta], next[index]];
    onChange(next);
  };
  return <fieldset className="editor">
    <legend className="field-label">User flow steps (run in order, in a fresh tab)</legend>
    {steps.map((step, index) => {
      const { needsSelector, needsValue } = flowAction(step.action);
      const selectorUsed = needsSelector || step.action === "press";
      return <div className="editor-row" key={index}>
        <span className="row-index">{index + 1}</span>
        <select aria-label={`Step ${index + 1} action`} value={step.action} onChange={(event) => onChange(updateAt(steps, index, { action: event.target.value }))}>
          {FLOW_ACTIONS.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
        {selectorUsed && <input aria-label={`Step ${index + 1} CSS selector`} placeholder={needsSelector ? "CSS selector, e.g. #email" : "optional selector"} maxLength={200} value={step.selector} onChange={(event) => onChange(updateAt(steps, index, { selector: event.target.value }))} />}
        {needsValue && <input aria-label={`Step ${index + 1} value`} type={step.action === "fill" && /pass/i.test(step.selector) ? "password" : "text"} placeholder={step.action === "press" ? "Enter" : step.action === "expect_url" ? "/dashboard" : "value"} maxLength={500} value={step.value} onChange={(event) => onChange(updateAt(steps, index, { value: event.target.value }))} />}
        <RowButtons index={index} count={steps.length} onMove={move} onRemove={(position) => onChange(steps.filter((_, i) => i !== position))} />
      </div>;
    })}
    <button type="button" className="text-button" disabled={steps.length >= MAX_STEPS} onClick={() => onChange([...steps, emptyStep()])}>+ Add step</button>
    <p className="fine-print">Typed values are sent to the page but never stored in the report; only their length is recorded. Form submissions are blocked, so a flow cannot change data on the site.</p>
  </fieldset>;
}

export function AssertionEditor({ rows, onChange }) {
  return <fieldset className="editor">
    <legend className="field-label">JSON assertions (dot paths such as data.items.0.id)</legend>
    {rows.map((row, index) => {
      const needsExpected = !["exists", "not_exists"].includes(row.operator);
      return <div className="editor-row" key={index}>
        <input aria-label={`Assertion ${index + 1} path`} placeholder="data.items.0.id" maxLength={200} value={row.path} onChange={(event) => onChange(updateAt(rows, index, { path: event.target.value }))} />
        <select aria-label={`Assertion ${index + 1} operator`} value={row.operator} onChange={(event) => onChange(updateAt(rows, index, { operator: event.target.value }))}>
          {ASSERTION_OPERATORS.map((operator) => <option key={operator} value={operator}>{operator.replace("_", " ")}</option>)}
        </select>
        {needsExpected && <input aria-label={`Assertion ${index + 1} expected value`} placeholder={row.operator === "type" ? "string / integer / array…" : row.operator === "length" ? "3" : "7, true, \"text\""} value={row.expected} onChange={(event) => onChange(updateAt(rows, index, { expected: event.target.value }))} />}
        <RowButtons index={index} count={rows.length} onRemove={(position) => onChange(rows.filter((_, i) => i !== position))} />
      </div>;
    })}
    <button type="button" className="text-button" disabled={rows.length >= MAX_ASSERTIONS} onClick={() => onChange([...rows, emptyAssertion()])}>+ Add assertion</button>
    <p className="fine-print">Numbers, true/false and null are compared as JSON values; anything else is compared as text.</p>
  </fieldset>;
}

export function HeaderEditor({ rows, onChange }) {
  return <fieldset className="editor">
    <legend className="field-label">Request headers (optional)</legend>
    {rows.map((row, index) => <div className="editor-row" key={index}>
      <input aria-label={`Header ${index + 1} name`} placeholder="Authorization" maxLength={64} value={row.name} onChange={(event) => onChange(updateAt(rows, index, { name: event.target.value }))} />
      <input aria-label={`Header ${index + 1} value`} type="password" autoComplete="off" placeholder="value" maxLength={2000} value={row.value} onChange={(event) => onChange(updateAt(rows, index, { value: event.target.value }))} />
      <RowButtons index={index} count={rows.length} onRemove={(position) => onChange(rows.filter((_, i) => i !== position))} />
    </div>)}
    <button type="button" className="text-button" disabled={rows.length >= MAX_HEADERS} onClick={() => onChange([...rows, emptyHeader()])}>+ Add header</button>
    <p className="fine-print">Header values are sent once and never stored or shown in reports.</p>
  </fieldset>;
}
