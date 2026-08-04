/**
 * Typed field wrappers — the helper the handoff asked for.
 *
 * Every control here renders FormField → FormItem → FormLabel → FormControl →
 * FormMessage exactly once, and is generic over the form's values so `name` is
 * checked against the schema rather than being a loose string. Call sites should
 * never hand-write that chain again.
 */
import type { ReactNode } from "react";
import type { Control, FieldPath, FieldValues } from "react-hook-form";
import { Combobox } from "@/components/ui/combobox";
import { Button } from "@/components/ui/button";
import { DatePicker, type DatePickerProps } from "@/components/ui/date-picker";
import { FormControl, FormField, FormItem, FormLabel, FormMessage } from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import type { FieldOption } from "@/lib/fieldOptions";

// toOptions lives in lib/fieldOptions so this module exports components only.
export type { FieldOption };

interface BaseFieldProps<T extends FieldValues, TOut extends FieldValues = T> {
  // Third arg is the schema's output type: a form whose zod schema transforms
  // (price strings -> numbers) has Control<In, ctx, Out>, and without it here
  // those forms cannot use these wrappers at all. `any` for context mirrors
  // react-hook-form's own ControllerProps.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  control: Control<T, any, TOut>;
  name: FieldPath<T>;
  label?: string;
  description?: string;
  disabled?: boolean;
  className?: string;
  labelClassName?: string;
  /** Rendered beside the label — ComparePanel slots its pin toggle here. */
  action?: ReactNode;
}

/** Label row shared by every field, so the `action` slot lines up identically. */
function FieldLabel({
  label,
  action,
  className,
}: {
  label?: string;
  action?: ReactNode;
  className?: string;
}) {
  if (!label && !action) return null;
  if (!action) return <FormLabel className={className}>{label}</FormLabel>;
  return (
    <div className="flex items-center gap-1">
      {label && <FormLabel className={className}>{label}</FormLabel>}
      {action}
    </div>
  );
}

function FieldDescription({ children }: { children?: ReactNode }) {
  if (!children) return null;
  return <p className="text-sm text-zinc-500">{children}</p>;
}

interface SelectFieldProps<T extends FieldValues, TOut extends FieldValues = T>
  extends BaseFieldProps<T, TOut> {
  options: FieldOption[];
  placeholder?: string;
  /**
   * Radix option values are always strings. Pass this when the field itself is
   * not a string — e.g. `side` is the number 1 or -1 — so the value written
   * back to the form still matches the schema.
   */
  parseValue?: (raw: string) => unknown;
}

export function SelectField<T extends FieldValues, TOut extends FieldValues = T>({
  control,
  name,
  label,
  description,
  disabled,
  className,
  labelClassName,
  action,
  options,
  placeholder,
  parseValue,
}: SelectFieldProps<T, TOut>) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem className={className}>
          <FieldLabel label={label} action={action} className={labelClassName} />
          <Select
            // Stringify for Radix, parse back for the schema.
            value={field.value == null ? "" : String(field.value)}
            onValueChange={(raw) => field.onChange(parseValue ? parseValue(raw) : raw)}
            disabled={disabled}
          >
            <FormControl>
              <SelectTrigger>
                <SelectValue placeholder={placeholder} />
              </SelectTrigger>
            </FormControl>
            <SelectContent>
              {options.map((option) => (
                <SelectItem key={option.value} value={option.value}>
                  {option.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <FieldDescription>{description}</FieldDescription>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}

interface InputFieldProps<T extends FieldValues, TOut extends FieldValues = T>
  extends BaseFieldProps<T, TOut> {
  placeholder?: string;
  type?: string;
  step?: string | number;
  min?: number;
  max?: number;
  inputMode?: "decimal" | "numeric" | "text";
  inputClassName?: string;
}

export function InputField<T extends FieldValues, TOut extends FieldValues = T>({
  control,
  name,
  label,
  description,
  disabled,
  className,
  labelClassName,
  action,
  inputClassName,
  ...input
}: InputFieldProps<T, TOut>) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem className={className}>
          <FieldLabel label={label} action={action} className={labelClassName} />
          <FormControl>
            <Input
              {...field}
              {...input}
              className={inputClassName}
              value={field.value ?? ""}
              disabled={disabled}
            />
          </FormControl>
          <FieldDescription>{description}</FieldDescription>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}

interface TextareaFieldProps<T extends FieldValues, TOut extends FieldValues = T>
  extends BaseFieldProps<T, TOut> {
  placeholder?: string;
  rows?: number;
}

export function TextareaField<T extends FieldValues, TOut extends FieldValues = T>({
  control,
  name,
  label,
  description,
  disabled,
  className,
  labelClassName,
  action,
  ...textarea
}: TextareaFieldProps<T, TOut>) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem className={className}>
          <FieldLabel label={label} action={action} className={labelClassName} />
          <FormControl>
            <Textarea {...field} {...textarea} value={field.value ?? ""} disabled={disabled} />
          </FormControl>
          <FieldDescription>{description}</FieldDescription>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}

/** Inline switch + clickable label. Was hand-rolled in three different wrappers. */
export function SwitchField<T extends FieldValues, TOut extends FieldValues = T>({
  control,
  name,
  label,
  description,
  disabled,
  className,
  labelClassName,
}: BaseFieldProps<T, TOut>) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem className={cn("space-y-0", className)}>
          <div className="flex items-center gap-2">
            <FormControl>
              <Switch
                checked={!!field.value}
                onCheckedChange={field.onChange}
                disabled={disabled}
              />
            </FormControl>
            {label && <FormLabel className={cn("cursor-pointer", labelClassName)}>{label}</FormLabel>}
          </div>
          <FieldDescription>{description}</FieldDescription>
        </FormItem>
      )}
    />
  );
}

interface DateFieldProps<T extends FieldValues, TOut extends FieldValues = T>
  extends BaseFieldProps<T, TOut> {
  placeholder?: string;
  /** Blinded sessions hide the date behind a mask. */
  masked?: boolean;
  disabledDays?: DatePickerProps["disabledDays"];
}

export function DateField<T extends FieldValues, TOut extends FieldValues = T>({
  control,
  name,
  label,
  description,
  disabled,
  className,
  labelClassName,
  action,
  ...picker
}: DateFieldProps<T, TOut>) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem className={className}>
          <FieldLabel label={label} action={action} className={labelClassName} />
          <FormControl>
            <DatePicker
              {...picker}
              value={field.value}
              onChange={field.onChange}
              disabled={disabled}
            />
          </FormControl>
          <FieldDescription>{description}</FieldDescription>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}

interface ComboboxFieldProps<T extends FieldValues, TOut extends FieldValues = T>
  extends BaseFieldProps<T, TOut> {
  options: FieldOption[];
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
}

export function ComboboxField<T extends FieldValues, TOut extends FieldValues = T>({
  control,
  name,
  label,
  description,
  disabled,
  className,
  labelClassName,
  action,
  ...combobox
}: ComboboxFieldProps<T, TOut>) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => (
        <FormItem className={className}>
          <FieldLabel label={label} action={action} className={labelClassName} />
          <FormControl>
            <Combobox
              {...combobox}
              value={field.value ?? ""}
              onChange={field.onChange}
              disabled={disabled}
            />
          </FormControl>
          <FieldDescription>{description}</FieldDescription>
          <FormMessage />
        </FormItem>
      )}
    />
  );
}

interface ChipToggleFieldProps<T extends FieldValues, TOut extends FieldValues = T>
  extends BaseFieldProps<T, TOut> {
  options: FieldOption[];
}

/**
 * Multi-select chips over a `string[]` field. Replaces two byte-identical copies
 * of this markup that had drifted into two different toggle implementations.
 */
export function ChipToggleField<T extends FieldValues, TOut extends FieldValues = T>({
  control,
  name,
  label,
  description,
  disabled,
  className,
  labelClassName,
  action,
  options,
}: ChipToggleFieldProps<T, TOut>) {
  return (
    <FormField
      control={control}
      name={name}
      render={({ field }) => {
        const selected: string[] = field.value ?? [];
        const toggle = (value: string) =>
          field.onChange(
            selected.includes(value)
              ? selected.filter((v) => v !== value)
              : [...selected, value],
          );

        return (
          <FormItem className={className}>
            <FieldLabel label={label} action={action} className={labelClassName} />
            <div className="flex flex-wrap gap-1.5">
              {options.map((option) => {
                const on = selected.includes(option.value);
                return (
                  <Button
                    key={option.value}
                    type="button"
                    variant={on ? "operator" : "outline"}
                    size="chip"
                    aria-pressed={on}
                    disabled={disabled}
                    onClick={() => toggle(option.value)}
                  >
                    {option.label}
                  </Button>
                );
              })}
            </div>
            <FieldDescription>{description}</FieldDescription>
            <FormMessage />
          </FormItem>
        );
      }}
    />
  );
}
