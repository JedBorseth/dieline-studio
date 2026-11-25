import { CircleAlertIcon, Trash2Icon, XIcon } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { cleanPathInput, formatModelSummary, parsePathList } from "../lib/chat";
import { request } from "../lib/engine";
import type { OpenRouterModel, SearchProvider, Settings, SettingsUpdate } from "../lib/protocol";
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
import { Textarea } from "./ui/textarea";

interface Props {
  settings: Settings;
  onClose: () => void;
  onSaved: (s: Settings) => void;
}

export function SettingsDialog({ settings, onClose, onSaved }: Props) {
  const [model, setModel] = useState(settings.model);
  const [effort, setEffort] = useState(settings.effort);
  const [outputDir, setOutputDir] = useState(settings.output_dir);
  const [dxfDir, setDxfDir] = useState(settings.dxf_dir ?? "");
  const [provider, setProvider] = useState<SearchProvider>(settings.search_provider ?? "exa");
  const [searchKeys, setSearchKeys] = useState<Partial<Record<SearchProvider, string>>>({});
  const [models, setModels] = useState<OpenRouterModel[] | null>(null);
  const [refDirs, setRefDirs] = useState((settings.reference_dirs ?? []).join("\n"));
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const modelInputRef = useRef<HTMLInputElement>(null);

  const save = async (update: SettingsUpdate, closeOnSuccess = true): Promise<boolean> => {
    if (saving) return false; // one write at a time: Save and Remove must not race
    setSaving(true);
    setError(null);
    try {
      onSaved(await request("settings.update", update));
      if (closeOnSuccess) onClose();
      return true;
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      return false;
    } finally {
      setSaving(false);
    }
  };

  // OpenRouter's live model list (tools + image input); the built-in suggestions if offline.
  useEffect(() => {
    let live = true;
    request("models.list", {})
      .then((r) => live && setModels(r.models))
      .catch(() => live && setModels(null));
    return () => {
      live = false;
    };
  }, []);
  const choices = models ?? settings.models.map((m) => ({ id: m.id, label: m.label }) as OpenRouterModel);
  const trimmedModel = model.trim();
  const chosen = models?.find((m) => m.id === trimmedModel);
  const fallbackLabel = settings.models.find((m) => m.id === trimmedModel)?.label;
  const providerItems = (settings.search_providers ?? ["exa", "tavily", "off"]).map((p) => ({
    value: p,
    label: p === "exa" ? "Exa" : p === "tavily" ? "Tavily" : "Off",
  }));
  const providerLabel = provider === "exa" ? "Exa" : "Tavily";
  const hasOwnKey = provider !== "off" && Boolean(settings.has_search_key?.[provider]);
  const searchKey = searchKeys[provider] ?? "";
  const effortItems = settings.efforts.map((x) => ({ value: x, label: x }));

  return (
    // Mounted only while open (see App), so the dialog is always open here.
    // While a write is pending the dialog stays open, so its outcome is always shown.
    <Dialog open onOpenChange={(open) => !open && !saving && onClose()}>
      <DialogPopup className="max-w-md" bottomStickOnMobile={false}>
        <form
          className="flex min-h-0 flex-col"
          onSubmit={(e) => {
            e.preventDefault();
            const update: SettingsUpdate = {
              model: trimmedModel,
              effort,
              output_dir: cleanPathInput(outputDir),
              dxf_dir: cleanPathInput(dxfDir),
              search_provider: provider,
              reference_dirs: parsePathList(refDirs),
            };
            if (apiKey.trim()) update.api_key = apiKey.trim();
            if (provider !== "off" && searchKey.trim()) update[`${provider}_api_key`] = searchKey.trim();
            void save(update, true);
          }}
        >
          <DialogHeader>
            <DialogTitle className="text-lg">Settings</DialogTitle>
            <DialogDescription>Chat model (via OpenRouter), API keys, folders and web search.</DialogDescription>
          </DialogHeader>

          <DialogPanel className="space-y-5">
            <div className="flex flex-col gap-2">
              <Label htmlFor="settings-api-key">OpenRouter API key</Label>
              <Input
                id="settings-api-key"
                type="password"
                autoComplete="off"
                aria-describedby="settings-api-key-help"
                placeholder={settings.has_api_key ? "Stored. Type to replace." : "sk-or-…"}
                value={apiKey}
                onChange={(e) => setApiKey(e.target.value)}
              />
              <div className="flex items-start justify-between gap-3">
                <p id="settings-api-key-help" className="text-muted-foreground text-xs">
                  Stored in Windows Credential Manager, never in a file. Usage is billed to this OpenRouter account.
                </p>
                {settings.has_api_key && (
                  <Button
                    variant="ghost-destructive"
                    size="xs"
                    className="-mt-0.5 shrink-0"
                    disabled={saving}
                    onClick={() => {
                      void save({ api_key: null }, false).then((ok) => ok && setApiKey(""));
                    }}
                  >
                    <Trash2Icon />
                    Remove stored key
                  </Button>
                )}
              </div>
            </div>

            <div className="grid grid-cols-1 gap-3 min-[400px]:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
              <div className="flex flex-col gap-2">
                <Label htmlFor="settings-model">Model (OpenRouter id)</Label>
                <div className="flex items-center gap-1">
                  <Input
                    ref={modelInputRef}
                    id="settings-model"
                    list="settings-model-list"
                    autoComplete="off"
                    spellCheck={false}
                    aria-describedby="settings-model-help"
                    value={model}
                    onChange={(e) => setModel(e.target.value)}
                  />
                  {model && (
                    <Button
                      variant="ghost"
                      size="icon-sm"
                      className="shrink-0 text-muted-foreground"
                      aria-label="Clear model to browse all models"
                      title="Clear to browse all models"
                      onClick={() => {
                        setModel("");
                        modelInputRef.current?.focus();
                      }}
                    >
                      <XIcon />
                    </Button>
                  )}
                </div>
                <datalist id="settings-model-list">
                  {choices.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label}
                    </option>
                  ))}
                </datalist>
                <p id="settings-model-help" className="text-muted-foreground text-xs wrap-anywhere">
                  {formatModelSummary(chosen, models !== null, fallbackLabel)}
                  {trimmedModel !== settings.model && " Start a new chat to use the new model."}
                </p>
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="settings-effort">Effort</Label>
                <Select items={effortItems} value={effort} onValueChange={(v) => v !== null && setEffort(v)}>
                  <SelectTrigger id="settings-effort" className="min-w-0">
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
                {settings.output_dir
                  ? "Leave empty to use the default export folder."
                  : `Leave empty for the default: ${settings.output_dir_resolved}`}
              </p>
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="settings-dxf-dir">Sample-table DXF folder</Label>
              <Input
                id="settings-dxf-dir"
                value={dxfDir}
                placeholder="Same as the export folder"
                onChange={(e) => setDxfDir(e.target.value)}
              />
              <p className="text-muted-foreground text-xs wrap-anywhere">
                Optional. DXF exports go here, e.g. your folder under Sample_Files on the design server.
              </p>
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="settings-ref-dirs">Reference folders (read-only)</Label>
              <Textarea
                id="settings-ref-dirs"
                value={refDirs}
                rows={3}
                placeholder={
                  settings.reference_dirs.length === 0 && settings.reference_dirs_resolved?.length
                    ? settings.reference_dirs_resolved.join("\n")
                    : "One folder per line"
                }
                onChange={(e) => setRefDirs(e.target.value)}
              />
              <p className="text-muted-foreground text-xs wrap-anywhere">
                The chat can search and read old CADs, specs and logs here, never change them. What it reads is sent
                through OpenRouter to the model's provider with the chat. One folder per line.{" "}
                {settings.reference_dirs.length > 0
                  ? "Leave empty to use your work OneDrive synced on this PC."
                  : `Leave empty for your work OneDrive synced on this PC${
                      settings.reference_dirs_resolved?.length
                        ? `: ${settings.reference_dirs_resolved.join(", ")}`
                        : " (none found)"
                    }.`}
              </p>
            </div>

            <div className="grid grid-cols-1 gap-3 min-[400px]:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
              <div className="flex flex-col gap-2">
                <Label htmlFor="settings-search">Web search</Label>
                <Select
                  items={providerItems}
                  value={provider}
                  onValueChange={(v) => v !== null && setProvider(v as SearchProvider)}
                >
                  <SelectTrigger id="settings-search" className="min-w-0">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectPopup>
                    {providerItems.map((p) => (
                      <SelectItem key={p.value} value={p.value}>
                        {p.label}
                      </SelectItem>
                    ))}
                  </SelectPopup>
                </Select>
              </div>
              {provider !== "off" && (
                <div className="flex flex-col gap-2">
                  <Label htmlFor="settings-search-key">{providerLabel} key (optional)</Label>
                  <Input
                    id="settings-search-key"
                    type="password"
                    autoComplete="off"
                    placeholder={hasOwnKey ? "Stored. Type to replace." : "Keyless (rate-limited)"}
                    value={searchKey}
                    onChange={(e) => setSearchKeys((prev) => ({ ...prev, [provider]: e.target.value }))}
                  />
                </div>
              )}
            </div>
            <div className="-mt-3 flex items-start justify-between gap-3">
              <p className="text-muted-foreground text-xs">
                {provider === "off"
                  ? "The chat can't search or fetch web pages."
                  : "Searches public facts like FEFCO styles or board specs, and fetches public pages (never local-network ones). Without a key the provider limits how often it can search."}
              </p>
              {hasOwnKey && (
                <Button
                  variant="ghost-destructive"
                  size="xs"
                  className="-mt-0.5 shrink-0"
                  disabled={saving}
                  onClick={() => {
                    const current = provider;
                    void save({ [`${current}_api_key`]: null } as SettingsUpdate, false).then(
                      (ok) => ok && setSearchKeys((prev) => ({ ...prev, [current]: "" })),
                    );
                  }}
                >
                  <Trash2Icon />
                  Remove {providerLabel} key
                </Button>
              )}
            </div>
          </DialogPanel>

          {error && (
            // Pinned above the footer so validation errors stay visible without scrolling the form.
            <div className="shrink-0 border-t px-6 py-3">
              <Alert variant="error">
                <CircleAlertIcon />
                <AlertDescription className="wrap-anywhere">{error}</AlertDescription>
              </Alert>
            </div>
          )}

          <DialogFooter className="flex-row justify-end">
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
