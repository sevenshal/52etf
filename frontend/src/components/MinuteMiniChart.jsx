import React, { useMemo } from 'react';
import { Empty, Spin, Typography } from 'antd';
import ReactECharts from 'echarts-for-react';

const { Text } = Typography;

const UP_COLOR = '#e5484d';
const DOWN_COLOR = '#2f9e63';
const BASE_COLOR = '#9aa3af';
const FULL_DAY_SLOTS = 241;   // 09:30 集合竞价 + 上午 120 + 下午 120

/**
 * 分时小图：上半部分时价格（相对前收的涨跌幅），下半部分成交量。
 *
 * - 单日模式（days=1）固定 241 个时间槽，曲线随时间向右延伸，盘中不会被拉伸变形。
 * - 多日模式把几天首尾相接，用分隔线与日期标签区分，纵轴仍是相对各自前收的涨跌幅。
 * - markLine 是 0 轴（前收位置），涨红跌绿。
 */
const MinuteMiniChart = ({ series, loading = false, height = 170, singleDay = false, highlightTime = null }) => {
  const option = useMemo(() => {
    const points = series?.points || [];
    const days = series?.days || [];
    if (!points.length || !days.length) return null;

    const useSlots = singleDay && days.length === 1;
    const categories = useSlots
      ? Array.from({ length: FULL_DAY_SLOTS }, (_, index) => points[index]?.time || '')
      : points.map(point => (point.time === '09:30' ? point.date.slice(5) : point.time));

    const pct = points.map(point => point.pct);
    const volume = points.map((point, index) => ({
      value: point.volume,
      itemStyle: {
        color: index > 0 && point.close < points[index - 1].close ? DOWN_COLOR : UP_COLOR,
        opacity: 0.55,
      },
    }));

    // 多日模式：每天的起点画一条竖分隔线
    const dayLines = !useSlots && days.length > 1
      ? days.slice(1).map(day => ({ xAxis: day.start_index }))
      : [];
    const hitIndex = highlightTime
      ? points.findIndex(point => point.time === highlightTime && point.date === series.today)
      : -1;

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
          axisLabel: { fontSize: 10, color: '#8b97a8', interval: Math.floor(categories.length / 5) },
          axisTick: { show: false },
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
        {
          type: 'value',
          gridIndex: 1,
          axisLabel: { show: false },
          splitLine: { show: false },
        },
      ],
      series: [
        {
          name: '涨跌幅',
          type: 'line',
          showSymbol: false,
          lineStyle: { width: 1.4, color: UP_COLOR },
          areaStyle: { color: 'rgba(229, 72, 77, 0.08)' },
          data: pct,
          markLine: {
            symbol: 'none',
            silent: true,
            lineStyle: { color: BASE_COLOR, width: 1, type: 'dashed' },
            label: { show: false },
            data: [{ yAxis: 0 }, ...dayLines],
          },
          markPoint: hitIndex >= 0 ? {
            symbol: 'circle',
            symbolSize: 7,
            itemStyle: { color: '#111827' },
            label: { show: false },
            data: [{ coord: [hitIndex, pct[hitIndex]], name: '命中' }],
          } : undefined,
        },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: volume,
        },
      ],
    };
  }, [series, height, singleDay, highlightTime]);

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
