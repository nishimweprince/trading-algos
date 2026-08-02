import { useForm } from "react-hook-form";
import { z } from "zod";
import { zodResolver } from "@hookform/resolvers/zod";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useSetups } from "@/hooks/useSetups";
import { useSubmitTrade } from "@/hooks/useTrades";
import { useCurrentBar } from "@/hooks/useReplay";
import type { Session } from "@/types";

const schema = z.object({
  setup_id: z.string().min(1),
  side: z.union([z.literal(1), z.literal(-1)]),
  entry: z.coerce.number().positive(),
  sl: z.coerce.number().positive(),
  tp: z.coerce.number().positive(),
  notes: z.string().optional(),
  calendar_flag: z.boolean().optional(),
  calendar_tags: z.string().optional(),
  observed_result: z.string().optional(),
});

type FormValues = z.infer<typeof schema>;

interface TradeFormProps {
  session: Session | null;
  blinded?: boolean;
  levels: { entry: number | null; sl: number | null; tp: number | null };
  onLevelsChange: (levels: { entry: number | null; sl: number | null; tp: number | null }) => void;
  dateFrom?: string;
  dateTo?: string;
}

export function TradeForm({ session, blinded, levels, onLevelsChange, dateFrom, dateTo }: TradeFormProps) {
  const currentBar = useCurrentBar();
  const { data: setups = [] } = useSetups();
  const submitTrade = useSubmitTrade();

  const { register, handleSubmit, setValue, watch, formState: { errors } } = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: {
      setup_id: "",
      side: 1,
      entry: levels.entry ?? undefined,
      sl: levels.sl ?? undefined,
      tp: levels.tp ?? undefined,
      calendar_flag: false,
    },
  });

  const side = watch("side");

  const onSubmit = handleSubmit(async (values) => {
    if (!session || !currentBar) return;
    await submitTrade.mutateAsync({
      session_id: session.session_id,
      symbol: session.symbol!,
      timeframe: session.timeframe!,
      signal_ts: currentBar.ts,
      setup_id: values.setup_id,
      side: values.side,
      entry: values.entry,
      sl: values.sl,
      tp: values.tp,
      notes: values.notes,
      calendar_flag: values.calendar_flag,
      calendar_tags: values.calendar_tags,
      observed_result: values.observed_result,
      date_from: dateFrom,
      date_to: dateTo,
    });
  });

  const syncLevel = (key: "entry" | "sl" | "tp", raw: string) => {
    const val = raw === "" ? null : Number(raw);
    onLevelsChange({ ...levels, [key]: val });
    if (val != null) setValue(key, val);
  };

  if (!session) {
    return (
      <Card>
        <CardHeader><CardTitle>Trade</CardTitle></CardHeader>
        <CardContent className="text-sm text-zinc-500">Start a session to label trades.</CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Mark Trade</CardTitle>
        {!blinded && currentBar && (
          <p className="text-xs text-zinc-500 font-mono">{currentBar.ts}</p>
        )}
      </CardHeader>
      <CardContent>
        <form onSubmit={onSubmit} className="space-y-3">
          <div className="space-y-1.5">
            <Label>Setup</Label>
            <Select onValueChange={(v) => setValue("setup_id", v)}>
              <SelectTrigger><SelectValue placeholder="Select setup" /></SelectTrigger>
              <SelectContent>
                {setups.map((s) => (
                  <SelectItem key={s.setup_id} value={s.setup_id}>{s.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            {errors.setup_id && <p className="text-xs text-red-400">{errors.setup_id.message}</p>}
          </div>

          <div className="space-y-1.5">
            <Label>Side</Label>
            <Select value={String(side)} onValueChange={(v) => setValue("side", Number(v) as 1 | -1)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">Long</SelectItem>
                <SelectItem value="-1">Short</SelectItem>
              </SelectContent>
            </Select>
          </div>

          <div className="grid grid-cols-3 gap-2">
            <div className="space-y-1.5">
              <Label>Entry</Label>
              <Input className="font-mono" type="number" step="any" defaultValue={levels.entry ?? ""} onChange={(e) => syncLevel("entry", e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Stop</Label>
              <Input className="font-mono" type="number" step="any" defaultValue={levels.sl ?? ""} onChange={(e) => syncLevel("sl", e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Target</Label>
              <Input className="font-mono" type="number" step="any" defaultValue={levels.tp ?? ""} onChange={(e) => syncLevel("tp", e.target.value)} />
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Switch id="calendar" onCheckedChange={(v) => setValue("calendar_flag", v)} />
            <Label htmlFor="calendar">High-impact news day</Label>
          </div>

          <div className="space-y-1.5">
            <Label>Calendar tags</Label>
            <Input {...register("calendar_tags")} placeholder="NFP, FOMC" />
          </div>

          <div className="space-y-1.5">
            <Label>Notes</Label>
            <Textarea {...register("notes")} placeholder="Discretionary notes…" />
          </div>

          <div className="space-y-1.5">
            <Label>Observed result (optional)</Label>
            <Input {...register("observed_result")} placeholder="Your read: win / loss" />
          </div>

          <Button type="submit" className="w-full" disabled={!currentBar || submitTrade.isPending}>
            {submitTrade.isPending ? "Submitting…" : "Submit Trade"}
          </Button>
          {submitTrade.isError && (
            <p className="text-xs text-red-400">{submitTrade.error.message}</p>
          )}
          {submitTrade.isSuccess && (
            <p className="text-xs text-emerald-400">Trade saved — result: {submitTrade.data.result}</p>
          )}
        </form>
      </CardContent>
    </Card>
  );
}
