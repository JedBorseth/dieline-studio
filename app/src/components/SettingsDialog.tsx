import { useEffect, useRef, useState } from "react";

import { request } from "../lib/engine";
import type { Settings, SettingsUpdate } from "../lib/protocol";

interface Props {
  settings: Settings;
  onClose: () => void;
  onSaved: (s: Settings) => void;
}

export function SettingsDialog({ settings, onClose, onSaved }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const [model, setModel] = useState(settings.model);
  const [effort, setEffort] = useState(settings.effort);
  const [outputDir, setOutputDir] = useState(settings.output_dir);
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  const save = async (update: SettingsUpdate) => {
    setSaving(true);
    setError(null);
    try {
      onSaved(await request("settings.update", update));
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <dialog ref={ref} className="dialog" onClose={onClose}>
      <form
        method="dialog"
        onSubmit={(e) => {
          e.preventDefault();
          const update: SettingsUpdate = { model, effort, output_dir: outputDir };
          if (apiKey.trim()) update.api_key = apiKey.trim();
          void save(update);
        }}
      >
        <h2>Settings</h2>

        <label className="field">
          Anthropic API key
          <input
            type="password"
            autoComplete="off"
            placeholder={settings.has_api_key ? "Stored. Type to replace." : "sk-ant-…"}
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
          />
          <small>Stored in Windows Credential Manager, never in a file. Billing goes to this key.</small>
        </label>
        {settings.has_api_key && (
          <button type="button" className="btn btn-ghost small" onClick={() => void save({ api_key: null })}>
            Remove stored key
          </button>
        )}

        <div className="field-row">
          <label className="field">
            Model
            <select value={model} onChange={(e) => setModel(e.target.value)}>
              {settings.models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            Effort
            <select value={effort} onChange={(e) => setEffort(e.target.value)}>
              {settings.efforts.map((x) => (
                <option key={x} value={x}>
                  {x}
                </option>
              ))}
            </select>
          </label>
        </div>

        <label className="field">
          Export folder
          <input
            value={outputDir}
            placeholder={settings.output_dir_resolved}
            onChange={(e) => setOutputDir(e.target.value)}
          />
          <small>Leave empty for the default: {settings.output_dir_resolved}</small>
        </label>

        {error && <div className="notice error">{error}</div>}

        <div className="dialog-actions">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={saving}>
            Save
          </button>
        </div>
      </form>
    </dialog>
  );
}
