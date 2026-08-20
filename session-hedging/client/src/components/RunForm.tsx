import { faPlay } from "@fortawesome/free-solid-svg-icons";
import { format } from "date-fns";
import type { ReactNode } from "react";
import { Controller, useFormContext, type UseFormRegisterReturn } from "react-hook-form";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { DatePicker } from "@/components/ui/date-picker";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Icon } from "@/lib/icon";
import { SESSION_LABEL, SESSIONS, TIMEFRAMES, type PerformanceUnit, type Timeframe } from "@/lib/types";

export type SourceChoice = "auto" | "local" | "ctrader";

export interface RunFormState {
  symbol: string;
  timeframe: Timeframe;
  dateFrom?: Date;
  dateTo?: Date;
  source: SourceChoice;
  sessions: string[];
  lockPips: number;
  slMult: number;
  rr: number;
  minStopPips: number;
  qty: number;
  performanceUnit: PerformanceUnit;
}

interface RunFormProps {
  loading: boolean;
  dollarsAvailable: boolean;
  onValid: (values: RunFormState) => void;
}

export const DEFAULT_FORM: RunFormState = {
  symbol: "XAUUSD",
  timeframe: "M15",
  source: "auto",
  sessions: [...SESSIONS],
  lockPips: 20,
  slMult: 2,
  rr: 3,
  minStopPips: 0,
  qty: 1,
  performanceUnit: "pips",
};

export function RunForm({ loading, dollarsAvailable, onValid }: RunFormProps) {
  const {
    register,
    control,
    handleSubmit,
    watch,
    formState: { errors },
  } = useFormContext<RunFormState>();
  const dateFrom = watch("dateFrom");
  const dateTo = watch("dateTo");

  return (
    <form className="flex flex-col gap-4" onSubmit={handleSubmit(onValid)} noValidate>
      <Field label="Symbol" htmlFor="symbol" error={errors.symbol?.message}>
        <Input
          id="symbol"
          {...register("symbol", {
            required: "Enter a symbol",
            setValueAs: (value: string) => value.trim().toUpperCase(),
            minLength: { value: 1, message: "Enter a symbol" },
          })}
        />
      </Field>
      <Field label="Timeframe" error={errors.timeframe?.message}>
        <Controller
          name="timeframe"
          control={control}
          rules={{ required: "Pick a timeframe" }}
          render={({ field }) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {TIMEFRAMES.map((tf) => (
                  <SelectItem key={tf} value={tf}>
                    {tf}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="From" error={errors.dateFrom?.message}>
          <Controller
            name="dateFrom"
            control={control}
            rules={{
              validate: (from) => {
                if (from && dateTo && from > dateTo) return "Start must be on or before end";
                return true;
              },
            }}
            render={({ field }) => (
              <DatePicker value={field.value} onChange={field.onChange} placeholder="Start" />
            )}
          />
        </Field>
        <Field label="To" error={errors.dateTo?.message}>
          <Controller
            name="dateTo"
            control={control}
            rules={{
              validate: (to) => {
                if (to && dateFrom && to < dateFrom) return "End must be on or after start";
                return true;
              },
            }}
            render={({ field }) => (
              <DatePicker value={field.value} onChange={field.onChange} placeholder="End" />
            )}
          />
        </Field>
      </div>
      <Field label="Source" error={errors.source?.message}>
        <Controller
          name="source"
          control={control}
          rules={{ required: "Pick a source" }}
          render={({ field }) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Auto (local if cached)</SelectItem>
                <SelectItem value="local">Local cache</SelectItem>
                <SelectItem value="ctrader">ctrader-markets</SelectItem>
              </SelectContent>
            </Select>
          )}
        />
      </Field>
      <Field label="Performance" error={errors.performanceUnit?.message}>
        <Controller
          name="performanceUnit"
          control={control}
          rules={{ required: "Pick a performance unit" }}
          render={({ field }) => (
            <Select value={field.value} onValueChange={field.onChange}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="pips">Pips</SelectItem>
                <SelectItem value="dollars" disabled={!dollarsAvailable}>
                  Dollars
                </SelectItem>
              </SelectContent>
            </Select>
          )}
        />
        {!dollarsAvailable ? (
          <p className="text-[11px] text-muted-foreground">
            Set DOLLARS_PER_PIP_PER_QTY to enable dollars.
          </p>
        ) : null}
      </Field>
      <Field label="Sessions in the run" error={errors.sessions?.message}>
        <Controller
          name="sessions"
          control={control}
          rules={{
            validate: (value) => value.length > 0 || "Pick at least one session",
          }}
          render={({ field }) => (
            <div className="flex flex-col gap-2">
              {SESSIONS.map((name) => (
                <label key={name} className="flex cursor-pointer items-center gap-2 text-xs">
                  <Checkbox
                    checked={field.value.includes(name)}
                    onCheckedChange={(checked) => {
                      if (checked) {
                        field.onChange([...SESSIONS.filter((session) => [...field.value, name].includes(session))]);
                        return;
                      }
                      const next = field.value.filter((session) => session !== name);
                      field.onChange(next);
                    }}
                  />
                  {SESSION_LABEL[name]}
                </label>
              ))}
            </div>
          )}
        />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <NumberField
          label="Lock pips"
          error={errors.lockPips?.message}
          registration={register("lockPips", nonNegative("Lock pips"))}
        />
        <NumberField
          label="SL × range"
          error={errors.slMult?.message}
          registration={register("slMult", positive("SL × range"))}
        />
        <NumberField label="R:R" error={errors.rr?.message} registration={register("rr", positive("R:R"))} />
        <NumberField
          label="Min stop pips"
          error={errors.minStopPips?.message}
          registration={register("minStopPips", nonNegative("Min stop pips"))}
        />
        <NumberField label="Qty" error={errors.qty?.message} registration={register("qty", positive("Qty"))} />
      </div>
      <Button type="submit" disabled={loading} className="mt-1 w-full">
        <Icon icon={faPlay} className="h-3 w-3" />
        {loading ? "Running…" : "Run backtest"}
      </Button>
      {(dateFrom || dateTo) && (
        <p className="text-[11px] text-muted-foreground">
          Range is UTC days
          {dateFrom ? ` from ${format(dateFrom, "d MMM yyyy")}` : ""}
          {dateTo ? ` to ${format(dateTo, "d MMM yyyy")}` : ""}.
        </p>
      )}
    </form>
  );
}

function positive(label: string) {
  return {
    valueAsNumber: true,
    required: `Enter ${label.toLowerCase()}`,
    validate: (value: number) =>
      (Number.isFinite(value) && value > 0) || `${label} must be greater than 0`,
  };
}

function nonNegative(label: string) {
  return {
    valueAsNumber: true,
    required: `Enter ${label.toLowerCase()}`,
    validate: (value: number) =>
      (Number.isFinite(value) && value >= 0) || `${label} must be 0 or more`,
  };
}

function Field({
  label,
  htmlFor,
  error,
  children,
}: {
  label: string;
  htmlFor?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
      {error ? <p className="text-[11px] text-loss">{error}</p> : null}
    </div>
  );
}

function NumberField({
  label,
  error,
  registration,
}: {
  label: string;
  error?: string;
  registration: UseFormRegisterReturn;
}) {
  return (
    <Field label={label} error={error}>
      <Input type="number" step="any" {...registration} />
    </Field>
  );
}
