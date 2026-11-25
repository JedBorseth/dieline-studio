import { CircleAlertIcon, Trash2Icon } from "lucide-react";
import { useState } from "react";

import { request } from "../lib/engine";
import type { Settings, SettingsUpdate } from "../lib/protocol";
import { Alert, AlertDescription } from "./ui/alert";
import { Button } from "./ui/button";
import {
  Dialog,
  DialogClose,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPanel,
  DialogPopup,
  DialogTitle,
} from "./ui/dialog";
import { Input } from "./ui/input";
import { Label } from "./ui/label";
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "./ui/select";
import { Spinner } from "./ui/spinner";

interface Props {
  settings: Settings;
  onClose: () => void;
  onSaved: (s: Settings) => void;
}

export function SettingsDialog({ settings, onClose, onSaved }: Props) {
  const [model, setModel] = useState(settings.model);
  const [effort, setEffort] = useState(settings.effort);
  const [outputDir, setOutputDir] = useState(settings.output_dir);
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const save = async (update: SettingsUpdate) => {
    if (saving) return; // one write at a time: Save and Remove must not race
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

  const modelItems = settings.models.map((m) => ({ value: m.id, label: m.label }));
  const effortItems = settings.efforts.map((x) => ({ value: x, label: x }));

  return (
    // Mounted only while open (see App), so the dialog is always open here.
    // While a write is pending the dialog stays open, so its outcome is always shown.
    <Dialog open onOpenChange={(open) => !open && !saving && onClose()}>
      <DialogPopup className="max-w-md">
        <form
          className="flex min-h-0 flex-col"
          onSubmit={(e) => {
            e.preventDefault();
            const update: SettingsUpdate = { model, effort, output_dir: outputDir };
            if (apiKey.trim()) update.api_key = apiKey.trim();
            void save(update);
          }}
        >
          <DialogHeader>
            <DialogTitle className="text-lg">Settings</DialogTitle>
            <DialogDescription>Chat model, API key and where exported CF2 files go.</DialogDescription>
          </DialogHeader>

          <DialogPanel className="space-y-5">
            <div className="flex flex-col gap-2">
              <Label htmlFor="settings-api-key">Anthropic API key</Label>
              <Input
                id="settings-api-key"
                type="password"
                autoComplete="off"
                placeholder={settings.has_api_key ? "Stored. Type to replace." : "sk-ant-…"}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
              <div className="flex items-start justify-between gap-3">
                <p className="text-muted-foreground text-xs">
                  Stored in Windows Credential Manager, never in a file. Billing goes to this key.
                </p>
                {settings.has_api_key && (
                  <Button
                    variant="ghost-destructive"
                    size="xs"
                    className="-mt-0.5 shrink-0"
                    disabled={saving}
                    onClick={() => void save({ api_key: null })}
                  >
                    <Trash2Icon />
                    Remove stored key
                  </Button>
                )}
              </div>
            </div>

            <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,1fr)] gap-3">
              <div className="flex flex-col gap-2">
                <Label>Model</Label>
                <Select items={modelItems} value={model} onValueChange={(v) => v !== null && setModel(v)}>
                  <SelectTrigger className="min-w-0" aria-label="Model">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectPopup>
                    {modelItems.map((m) => (
                      <SelectItem key={m.value} value={m.value}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectPopup>
                </Select>
              </div>
              <div className="flex flex-col gap-2">
                <Label>Effort</Label>
                <Select items={effortItems} value={effort} onValueChange={(v) => v !== null && setEffort(v)}>
                  <SelectTrigger className="min-w-0" aria-label="Effort">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectPopup>
                    {effortItems.map((x) => (
                      <SelectItem key={x.value} value={x.value}>
                        {x.label}
                      </SelectItem>
                    ))}
                  </SelectPopup>
                </Select>
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="settings-output-dir">Export folder</Label>
              <Input
                id="settings-output-dir"
                value={outputDir}
                placeholder={settings.output_dir_resolved}
                onChange={(e) => setOutputDir(e.target.value)}
              />
              <p className="text-muted-foreground text-xs wrap-anywhere">
                Leave empty for the default: {settings.output_dir_resolved}
              </p>
            </div>

            {error && (
              <Alert variant="error">
                <CircleAlertIcon />
                <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
              </Alert>
            )}
          </DialogPanel>

          <DialogFooter>
            <DialogClose render={<Button variant="outline" disabled={saving} />}>Cancel</DialogClose>
            <Button type="submit" disabled={saving}>
              {saving && <Spinner />}
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogPopup>
    </Dialog>
  );
}
