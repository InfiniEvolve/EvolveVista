// The interface language, TraceVista's: its switch, its stored choice and its Chinese table. EvolveVista's own
// strings are in i18n.zh.json beside this file; a key missing there falls back to TraceVista's table, then to
// the English key, so the code still reads as English.
import { useMemo } from 'react';
import { translate as tracevista, useLang as useTraceVistaLang, type Lang } from '@tracevista/i18n';
import zh from './i18n.zh.json';

export { LangProvider, LangToggle } from '@tracevista/i18n';
export type { Lang } from '@tracevista/i18n';

type Vars = Record<string, string | number>;
const own: Partial<Record<Lang, Record<string, string>>> = { zh: zh as Record<string, string> };
const fill = (text: string, vars?: Vars) => vars ? text.replace(/\{(\w+)\}/g, (m, k: string) => vars[k] != null ? String(vars[k]) : m) : text;

export function translate(lang: Lang, key: string, vars?: Vars): string {
  const text = own[lang]?.[key];
  return text != null ? fill(text, vars) : tracevista(lang, key, vars);
}

export function useLang() {
  const value = useTraceVistaLang();
  return useMemo(() => ({ ...value, t: (key: string, vars?: Vars) => translate(value.lang, key, vars) }), [value]);
}
