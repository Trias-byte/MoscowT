import { useLayoutEffect, useRef } from 'react';
const offsets = new Map<string, number>();
/** Retain scroll when an expensive panel is unmounted while hidden. */
export function useRetainedScroll(key: string) {
  const ref = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    element.scrollTop = offsets.get(key) ?? 0;
    return () => {
      offsets.set(key, element.scrollTop);
    };
  }, [key]);
  return ref;
}
