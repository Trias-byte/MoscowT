import { useEffect, useRef } from 'react';
import * as echarts from 'echarts/core';
import { LineChart, BarChart, HeatmapChart } from 'echarts/charts';
import {
  GridComponent,
  TooltipComponent,
  MarkLineComponent,
  VisualMapComponent,
  AriaComponent,
  LegendComponent,
  MarkAreaComponent,
} from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import type { EChartsCoreOption } from 'echarts/core';
echarts.use([
  LineChart,
  BarChart,
  HeatmapChart,
  GridComponent,
  TooltipComponent,
  MarkLineComponent,
  VisualMapComponent,
  AriaComponent,
  LegendComponent,
  MarkAreaComponent,
  CanvasRenderer,
]);
export function Chart({
  option,
  label,
  onSelect,
  className = '',
}: {
  option: EChartsCoreOption;
  label: string;
  onSelect?: (index: number, value: unknown) => void;
  className?: string;
}) {
  const element = useRef<HTMLDivElement>(null),
    instance = useRef<echarts.ECharts | null>(null),
    callback = useRef(onSelect);
  callback.current = onSelect;
  useEffect(() => {
    const chart = echarts.init(element.current!);
    instance.current = chart;
    chart.on('click', (p) => callback.current?.(p.dataIndex, p.value));
    const resize = new ResizeObserver(() => chart.resize());
    resize.observe(element.current!);
    return () => {
      resize.disconnect();
      chart.dispose();
      instance.current = null;
    };
  }, []);
  useEffect(() => {
    instance.current?.setOption(
      {
        ...option,
        animation: false,
        textStyle: { fontFamily: 'Inter, Arial, sans-serif', fontSize: 11, color: '#84918e' },
        aria: { enabled: true, label: { description: label }, decal: { show: true } },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          backgroundColor: '#ffffff',
          borderColor: '#e2e9e6',
          textStyle: { color: '#263e37', fontSize: 12 },
          ...((option.tooltip as object) || {}),
        },
      },
      true,
    );
  }, [option, label]);
  return <div ref={element} className={`chart ${className}`} role="img" aria-label={label} />;
}
