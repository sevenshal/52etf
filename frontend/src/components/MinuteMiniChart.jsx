import React, { useMemo } from 'react';
import { Empty, Spin, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';

const { Text } = Typography;

const UP_COLOR = '#e5484d';
const DOWN_COLOR = '#2f9e63';
const BASE_COLOR = '#9aa3af';
const FULL_DAY_SLOTS = 241;   // 09:30 集合竞价 + 上午 120 + 下午 120
// 信号标记颜色：与标签体系一致（强势深红、活跃浅红、观望浅绿、规避深绿）
export const SIGNAL_COLORS = { 强势: '#c62828', 活跃: '#f08a5d', 观望: '#6cc087', 规避: '#0a7a38' };

/** 当日涨跌：最后一个有效价对比前收，缺前收时按涨处理 */
const dayColor = day => (day?.pre_close != null && day?.close != null && day.close < day.pre_close ? DOWN_COLOR : UP_COLOR);

/**
 * 分时小图：上半分时价格（相对前收的涨跌幅），下半成交量。
 *
 * - 单日模式固定 241 个时间槽，曲线随时间向右延伸，盘中不会被拉伸变形。
 * - 多日模式每天画一条独立曲线：按当日涨跌着色（收阴绿、收阳红），当日那条加粗且不淡化，
 *   横轴只在每天第一根显示日期，日与日之间画竖分隔线。
 */
const MinuteMiniChart = ({
  series,
  loading = false,
  height = 170,
  singleDay = false,
  highlightTime = null,
  events = null,         // 信号变更流水：[{date, time, label, prev_label}]，全部按类型着色标出
}) => {
  const option = useMemo(() => {
    const points = series?.points || [];
    const days = series?.days || [];
    if (!points.length || !days.length) return null;

    const multi = !singleDay && days.length > 1;
    const useSlots = singleDay || days.length === 1;
    const categories = useSlots
      ? Array.from({ length: FULL_DAY_SLOTS }, (_, index) => points[index]?.time || '')
      : points.map(point => point.time);
    const dayStarts = days.map(day => day.start_index);
    const lastDayDate = days[days.length - 1]?.date;

    const volume = points.map((point, index) => {
      const day = days.find(item => item.date === point.date);
      const isToday = point.date === lastDayDate;
      return {
        value: point.volume,
        itemStyle: { color: dayColor(day), opacity: multi && !isToday ? 0.3 : 0.5 },
      };
    });

    // 多日：每天一条曲线，其余位置补 null；单日：一条曲线
    const priceSeries = useSlots
      ? [{
        name: days[0]?.date || '分时',
        type: 'line',
        showSymbol: false,
        connectNulls: false,
        lineStyle: { width: 1.6, color: dayColor(days[0]) },
        itemStyle: { color: dayColor(days[0]) },
        areaStyle: { color: dayColor(days[0]) === UP_COLOR ? 'rgba(229,72,77,0.08)' : 'rgba(47,158,99,0.08)' },
        data: points.map(point => point.pct),
      }]
      : days.map(day => {
        const isToday = day.date === lastDayDate;
        const data = new Array(points.length).fill(null);
        for (let offset = 0; offset < day.count; offset += 1) {
          data[day.start_index + offset] = points[day.start_index + offset]?.pct ?? null;
        }
        return {
          name: day.date.slice(5),
          type: 'line',
          showSymbol: false,
          connectNulls: false,
          lineStyle: { color: dayColor(day), width: isToday ? 2 : 1.1, opacity: isToday ? 1 : 0.5 },
          itemStyle: { color: dayColor(day) },
          z: isToday ? 3 : 2,
          data,
        };
      });

    // 信号标记：每次出现/变化都标一个点，颜色按标签类型；多日模式标到对应那天的曲线上
    const indexOf = (date, time) => points.findIndex(point => point.date === date && point.time === time);
    const signalMarks = (events || [])
      .map(event => ({ event, index: indexOf(event.date, event.time) }))
      .filter(item => item.index >= 0)
      .map(({ event, index }) => ({
        coord: [index, points[index].pct],
        name: event.label,
        value: event.prev_label ? `${event.prev_label}→${event.label}` : event.label,
        itemStyle: { color: SIGNAL_COLORS[event.label] || '#111827', borderColor: '#fff', borderWidth: 1 },
        date: event.date,
      }));
    if (!signalMarks.length && highlightTime) {
      const hitIndex = indexOf(series.today, highlightTime);
      if (hitIndex >= 0) {
        signalMarks.push({
          coord: [hitIndex, points[hitIndex].pct],
          name: '命中',
          itemStyle: { color: '#111827' },
          date: series.today,
        });
      }
    }
    if (signalMarks.length) {
      // 标到各自那天的曲线上，保证淡化的历史日上的点也在正确的 series 里
      const seriesByDate = useSlots ? null : Object.fromEntries(days.map((day, i) => [day.date, priceSeries[i]]));
      signalMarks.forEach(mark => {
        const target = useSlots ? priceSeries[0] : (seriesByDate[mark.date] || priceSeries[priceSeries.length - 1]);
        target.markPoint = target.markPoint || {
          symbol: 'circle',
          symbolSize: 8,
          label: { show: false },
          tooltip: { formatter: item => `${item.data.date} 信号：${item.data.value || item.name}` },
          data: [],
        };
        target.markPoint.data.push(mark);
      });
    }
    priceSeries[0].markLine = {
      symbol: 'none',
      silent: true,
      lineStyle: { color: BASE_COLOR, width: 1, type: 'dashed' },
      label: { show: false },
      data: [
        { yAxis: 0 },
        ...(multi ? dayStarts.filter(index => index > 0).map(index => ({ xAxis: index })) : []),
      ],
    };

    return {
      animation: false,
      grid: [
        { left: 46, right: 46, top: 12, height: height * 0.56 },
        { left: 46, right: 46, top: height * 0.56 + 26, height: height * 0.22 },
      ],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      tooltip: {
        trigger: 'axis',
        confine: true,
        formatter: params => {
          const index = params[0]?.dataIndex ?? 0;
          const point = points[index];
          if (!point) return '';
          return `${point.date} ${point.time}<br/>价 ${point.close} · ${point.pct > 0 ? '+' : ''}${point.pct?.toFixed(2)}%`
            + `<br/>量 ${Math.round(point.volume).toLocaleString()}`;
        },
      },
      xAxis: [
        {
          type: 'category',
          data: categories,
          boundaryGap: false,
          axisTick: { show: false },
          axisLabel: multi
            ? {
              // 只在每天第一根显示，标的是日期而不是时分
              interval: index => dayStarts.includes(index),
              formatter: (value, index) => (points[index] ? points[index].date.slice(5) : value),
              fontSize: 10,
              color: '#61708a',
              fontWeight: 600,
            }
            : { fontSize: 10, color: '#8b97a8', interval: Math.floor(categories.length / 5) },
        },
        {
          type: 'category',
          gridIndex: 1,
          data: categories,
          boundaryGap: false,
          axisLabel: { show: false },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          type: 'value',
          scale: true,
          axisLabel: { fontSize: 10, formatter: value => `${value.toFixed(1)}%`, color: '#8b97a8' },
          splitLine: { lineStyle: { color: '#f2f4f7' } },
        },
        { type: 'value', gridIndex: 1, axisLabel: { show: false }, splitLine: { show: false } },
      ],
      series: [
        ...priceSeries,
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: volume },
      ],
    };
  }, [series, height, singleDay, highlightTime, events]);

  if (loading) {
    return <div className="minute-mini minute-mini--loading"><Spin size="small" /></div>;
  }
  if (!option) {
    return (
      <div className="minute-mini minute-mini--empty">
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={<Text type="secondary">暂无分时数据</Text>} />
      </div>
    );
  }
  return <ReactECharts option={option} style={{ height }} notMerge lazyUpdate />;
};

export default MinuteMiniChart;
