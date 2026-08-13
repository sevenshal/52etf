import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  List,
  Modal,
  Popconfirm,
  Select,
  Segmented,
  Space,
  Table,
  Tag,
  Tooltip,
  Typography,
  message,
} from 'antd';
import {
  BarChartOutlined,
  ClearOutlined,
  CodeOutlined,
  DatabaseOutlined,
  DeleteOutlined,
  DotChartOutlined,
  HeatMapOutlined,
  LineChartOutlined,
  PieChartOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
  StarOutlined,
  TableOutlined,
} from '@ant-design/icons';
import ReactECharts from 'echarts-for-react';
import request from '../utils/request';
import './DatabaseManager.css';

const { Text, Title } = Typography;
const { TextArea } = Input;

const chartTypeOptions = [
  { label: '柱状图', value: 'bar', icon: <BarChartOutlined /> },
  { label: '折线图', value: 'line', icon: <LineChartOutlined /> },
  { label: '点图', value: 'scatter', icon: <DotChartOutlined /> },
  { label: '饼图', value: 'pie', icon: <PieChartOutlined /> },
  { label: '热力图', value: 'heatmap', icon: <HeatMapOutlined /> },
];

const queryEngineOptions = [
  { label: 'DuckDB分析', value: 'duckdb' },
  { label: 'SQLite只读', value: 'sqlite' },
];

const getErrorMessage = (error, fallback) => {
  const detail = error?.response?.data?.detail || error?.response?.data?.message || error?.message;
  if (!detail) return fallback;
  if (typeof detail === 'string') return detail;
  return JSON.stringify(detail);
};

const isNumeric = (value) => {
  if (value === null || value === undefined || value === '') return false;
  return Number.isFinite(Number(value));
};

const formatCellValue = (value) => {
  if (value === null || value === undefined) return '-';
  if (typeof value === 'object') {
    if (value.type === 'binary') return `<binary ${value.size} bytes>`;
    return JSON.stringify(value);
  }
  return String(value);
};

const formatTiming = (value) => {
  if (value === null || value === undefined) return '-';
  return `${Number(value).toFixed(1)} ms`;
};

const formatChartValue = (value) => (
  Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 6 })
);

const escapeTooltipText = (value) => String(value).replace(/[&<>"']/g, char => ({
  '&': '&amp;',
  '<': '&lt;',
  '>': '&gt;',
  '"': '&quot;',
  "'": '&#39;',
}[char]));

const getCurrentToken = (text, caretPosition) => {
  const beforeCaret = text.slice(0, caretPosition);
  const match = beforeCaret.match(/([A-Za-z_][A-Za-z0-9_.$]*)$/);
  if (!match) return null;
  return {
    value: match[1],
    start: caretPosition - match[1].length,
    end: caretPosition,
  };
};

const SqlEditorCard = React.memo(({
  favoriteDeleting,
  favoriteOptions,
  favoritesLoading,
  maxLimit,
  queryEngine,
  queryLoading,
  selectedFavoriteId,
  sqlSeed,
  suggestionSource,
  onDeleteFavorite,
  onEngineChange,
  onFavoriteClear,
  onFavoriteSelect,
  onOpenSaveFavorite,
  onRun,
}) => {
  const [sql, setSql] = useState('');
  const [suggestions, setSuggestions] = useState([]);
  const [activeSuggestionIndex, setActiveSuggestionIndex] = useState(0);
  const textAreaRef = useRef(null);

  useEffect(() => {
    if (sqlSeed?.sql !== undefined) {
      setSql(sqlSeed.sql);
      setSuggestions([]);
      setActiveSuggestionIndex(0);
    }
  }, [sqlSeed]);

  const refreshSuggestions = (nextSql, caretPosition) => {
    const token = getCurrentToken(nextSql, caretPosition);
    if (!token || token.value.length < 1) {
      if (suggestions.length) {
        setSuggestions([]);
        setActiveSuggestionIndex(0);
      }
      return;
    }

    const keyword = token.value.toLowerCase();
    const ranked = suggestionSource
      .filter(item => item.searchValue.includes(keyword))
      .sort((left, right) => {
        const leftStarts = left.searchValue.startsWith(keyword) ? 0 : 1;
        const rightStarts = right.searchValue.startsWith(keyword) ? 0 : 1;
        if (leftStarts !== rightStarts) return leftStarts - rightStarts;
        if (left.kind !== right.kind) return left.kind === '表' ? -1 : 1;
        return left.value.localeCompare(right.value);
      })
      .slice(0, 10);

    setSuggestions(ranked);
    setActiveSuggestionIndex(0);
  };

  const getTextArea = () => textAreaRef.current?.resizableTextArea?.textArea;

  const applySuggestion = (suggestion) => {
    const textArea = getTextArea();
    if (!textArea || !suggestion) return;

    const caretPosition = textArea.selectionStart || 0;
    const token = getCurrentToken(sql, caretPosition);
    if (!token) return;

    const nextSql = `${sql.slice(0, token.start)}${suggestion.value}${sql.slice(token.end)}`;
    const nextCaret = token.start + suggestion.value.length;
    setSql(nextSql);
    setSuggestions([]);

    window.requestAnimationFrame(() => {
      textArea.focus();
      textArea.setSelectionRange(nextCaret, nextCaret);
    });
  };

  const handleSqlChange = (event) => {
    const nextSql = event.target.value;
    setSql(nextSql);
    refreshSuggestions(nextSql, event.target.selectionStart || nextSql.length);
  };

  const handleSqlKeyDown = (event) => {
    if (!suggestions.length) return;

    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveSuggestionIndex(index => (index + 1) % suggestions.length);
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveSuggestionIndex(index => (index - 1 + suggestions.length) % suggestions.length);
    } else if (event.key === 'Enter' || event.key === 'Tab') {
      event.preventDefault();
      applySuggestion(suggestions[activeSuggestionIndex]);
    } else if (event.key === 'Escape') {
      setSuggestions([]);
    }
  };

  return (
    <Card
      className="db-editor-card"
      title={(
        <Space>
          <CodeOutlined />
          <Title level={5} style={{ margin: 0 }}>SQL 查询</Title>
        </Space>
      )}
      extra={(
        <Space wrap className="db-editor-toolbar">
          <Select
            allowClear
            showSearch
            className="db-favorite-select"
            value={selectedFavoriteId || undefined}
            loading={favoritesLoading}
            placeholder="收藏夹"
            options={favoriteOptions}
            optionFilterProp="label"
            onChange={value => {
              if (value) {
                onFavoriteSelect(value);
              } else {
                onFavoriteClear();
              }
            }}
          />
          <Button
            icon={<StarOutlined />}
            onClick={() => onOpenSaveFavorite(sql)}
          >
            收藏
          </Button>
          <Popconfirm
            title="删除这个SQL收藏？"
            okText="删除"
            cancelText="取消"
            disabled={!selectedFavoriteId}
            onConfirm={onDeleteFavorite}
          >
            <Button
              danger
              icon={<DeleteOutlined />}
              disabled={!selectedFavoriteId}
              loading={favoriteDeleting}
            />
          </Popconfirm>
          <Select
            className="db-engine-select"
            value={queryEngine}
            options={queryEngineOptions}
            onChange={onEngineChange}
          />
          <Button
            icon={<ClearOutlined />}
            onClick={() => {
              setSql('');
              setSuggestions([]);
              onFavoriteClear();
            }}
          >
            清空
          </Button>
          <Button
            type="primary"
            icon={<PlayCircleOutlined />}
            loading={queryLoading}
            onClick={() => onRun(sql)}
          >
            执行查询
          </Button>
        </Space>
      )}
    >
      <Alert
        showIcon
        type="info"
        message={`${queryEngine === 'duckdb' ? 'DuckDB分析模式支持GROUP BY、JOIN和窗口函数；' : 'SQLite只读模式；'}仅允许SELECT查询；只能读取无account_id字段的数据表；未写LIMIT时后端自动限制${maxLimit}行，超过${maxLimit}行会被截断。`}
        className="db-query-alert"
      />
      <TextArea
        ref={textAreaRef}
        className="db-sql-editor"
        value={sql}
        onChange={handleSqlChange}
        onKeyDown={handleSqlKeyDown}
        onClick={() => {
          const textArea = getTextArea();
          refreshSuggestions(sql, textArea?.selectionStart || sql.length);
        }}
        placeholder="SELECT * FROM stock_evc LIMIT 100"
        rows={8}
      />
      {!!suggestions.length && (
        <div className="db-suggestions">
          {suggestions.map((suggestion, index) => (
            <button
              key={`${suggestion.kind}-${suggestion.value}`}
              type="button"
              className={index === activeSuggestionIndex ? 'active' : ''}
              onMouseDown={event => {
                event.preventDefault();
                applySuggestion(suggestion);
              }}
            >
              <Tag color={suggestion.kind === '表' ? 'blue' : 'green'}>{suggestion.kind}</Tag>
              <span className="db-suggestion-name">{suggestion.label}</span>
              <span className="db-suggestion-detail">{suggestion.detail}</span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
});

const DatabaseManager = () => {
  const [tables, setTables] = useState([]);
  const [maxLimit, setMaxLimit] = useState(500);
  const [schemaLoading, setSchemaLoading] = useState(false);
  const [selectedTableName, setSelectedTableName] = useState(null);
  const [tableSearch, setTableSearch] = useState('');
  const [sqlSeed, setSqlSeed] = useState({ id: 0, sql: '' });
  const [favorites, setFavorites] = useState([]);
  const [favoritesLoading, setFavoritesLoading] = useState(false);
  const [favoriteSaving, setFavoriteSaving] = useState(false);
  const [favoriteDeleting, setFavoriteDeleting] = useState(false);
  const [selectedFavoriteId, setSelectedFavoriteId] = useState();
  const [favoriteModalOpen, setFavoriteModalOpen] = useState(false);
  const [favoriteName, setFavoriteName] = useState('');
  const [pendingFavoriteSql, setPendingFavoriteSql] = useState('');
  const [queryEngine, setQueryEngine] = useState('duckdb');
  const [queryLoading, setQueryLoading] = useState(false);
  const [result, setResult] = useState(null);
  const [viewMode, setViewMode] = useState('table');
  const [chartType, setChartType] = useState('bar');
  const [dimensionColumn, setDimensionColumn] = useState();
  const [heatmapYColumn, setHeatmapYColumn] = useState();
  const [valueColumn, setValueColumn] = useState();

  const selectedTable = useMemo(
    () => tables.find(table => table.name === selectedTableName) || null,
    [tables, selectedTableName]
  );

  const selectedFavorite = useMemo(
    () => favorites.find(favorite => favorite.id === selectedFavoriteId) || null,
    [favorites, selectedFavoriteId]
  );

  const favoriteOptions = useMemo(() => (
    favorites.map(favorite => ({
      value: favorite.id,
      label: favorite.name,
    }))
  ), [favorites]);

  const suggestionSource = useMemo(() => {
    const columnSuggestions = [];
    const columnSeen = new Set();

    tables.forEach(table => {
      table.columns.forEach(column => {
        const key = column.name.toLowerCase();
        if (!columnSeen.has(key)) {
          columnSeen.add(key);
          columnSuggestions.push({
            value: column.name,
            label: column.name,
            searchValue: column.name.toLowerCase(),
            kind: '字段',
            detail: `${table.name} · ${column.type || 'unknown'}`,
          });
        }
      });
    });

    return [
      ...tables.map(table => ({
        value: table.name,
        label: table.name,
        searchValue: table.name.toLowerCase(),
        kind: '表',
        detail: `${table.column_count}个字段`,
      })),
      ...columnSuggestions,
    ];
  }, [tables]);

  const filteredTables = useMemo(() => {
    const keyword = tableSearch.trim().toLowerCase();
    if (!keyword) return tables;
    return tables.filter(table => (
      table.name.toLowerCase().includes(keyword)
      || table.columns.some(column => column.name.toLowerCase().includes(keyword))
    ));
  }, [tables, tableSearch]);

  const resultColumns = useMemo(() => result?.columns || [], [result]);
  const resultRows = useMemo(() => result?.rows || [], [result]);

  const numericColumns = useMemo(() => (
    resultColumns.filter(column => resultRows.some(row => isNumeric(row[column])))
  ), [resultColumns, resultRows]);

  const tableColumns = useMemo(() => (
    resultColumns.map(column => ({
      title: column,
      dataIndex: column,
      key: column,
      width: 168,
      ellipsis: true,
      render: value => (
        <Tooltip title={formatCellValue(value)}>
          <span>{formatCellValue(value)}</span>
        </Tooltip>
      ),
    }))
  ), [resultColumns]);

  const tableDataSource = useMemo(() => (
    resultRows.map((row, index) => ({ ...row, __rowIndex: index }))
  ), [resultRows]);

  const timingItems = useMemo(() => {
    const timings = result?.timings || {};
    return [
      ['总耗时', 'request_total_ms'],
      ['Schema', 'schema_ms'],
      ['Setup', 'setup_ms'],
      ['执行', 'execute_ms'],
      ['拉取', 'fetch_ms'],
      ['序列化', 'serialize_ms'],
    ]
      .filter(([, key]) => timings[key] !== undefined)
      .map(([label, key]) => ({ label, value: timings[key] }));
  }, [result]);

  const chartOption = useMemo(() => {
    if (!resultRows.length || !dimensionColumn || !valueColumn) return null;

    if (chartType === 'heatmap') {
      if (!heatmapYColumn) return null;

      const xLabels = [];
      const yLabels = [];
      const xLabelSet = new Set();
      const yLabelSet = new Set();
      const heatmapRows = resultRows
        .map(row => {
          const xLabel = formatCellValue(row[dimensionColumn]);
          const yLabel = formatCellValue(row[heatmapYColumn]);
          const value = Number(row[valueColumn]);
          if (!Number.isFinite(value)) return null;

          if (!xLabelSet.has(xLabel)) {
            xLabelSet.add(xLabel);
            xLabels.push(xLabel);
          }
          if (!yLabelSet.has(yLabel)) {
            yLabelSet.add(yLabel);
            yLabels.push(yLabel);
          }

          return { xLabel, yLabel, value };
        })
        .filter(Boolean);

      if (!heatmapRows.length) return null;

      const xIndexMap = new Map(xLabels.map((label, index) => [label, index]));
      const yIndexMap = new Map(yLabels.map((label, index) => [label, index]));
      const cellValueMap = new Map();

      heatmapRows.forEach(row => {
        const xIndex = xIndexMap.get(row.xLabel);
        const yIndex = yIndexMap.get(row.yLabel);
        const key = `${xIndex}-${yIndex}`;
        cellValueMap.set(key, (cellValueMap.get(key) || 0) + row.value);
      });

      const cellValues = Array.from(cellValueMap.values());
      const minValue = Math.min(...cellValues);
      const maxValue = Math.max(...cellValues);
      const visualPadding = Math.abs(maxValue || 1);
      const visualMin = minValue === maxValue ? minValue - visualPadding : minValue;
      const visualMax = minValue === maxValue ? maxValue + visualPadding : maxValue;
      const heatmapData = Array.from(cellValueMap.entries()).map(([key, value]) => {
        const [xIndex, yIndex] = key.split('-').map(Number);
        return [xIndex, yIndex, value];
      });

      return {
        tooltip: {
          position: 'top',
          formatter: params => {
            const point = Array.isArray(params) ? params[0] : params;
            const pointValue = Array.isArray(point?.value) ? point.value : point?.data;
            if (!Array.isArray(pointValue) || pointValue.length < 3) {
              return '';
            }

            const [xIndex, yIndex, value] = pointValue;
            return [
              `${escapeTooltipText(dimensionColumn)}: ${escapeTooltipText(xLabels[xIndex])}`,
              `${escapeTooltipText(heatmapYColumn)}: ${escapeTooltipText(yLabels[yIndex])}`,
              `${escapeTooltipText(valueColumn)}: ${formatChartValue(value)}`,
            ].join('<br/>');
          },
        },
        grid: { left: 96, right: 56, top: 36, bottom: 96 },
        dataZoom: [
          { type: 'inside', xAxisIndex: 0 },
          { type: 'slider', xAxisIndex: 0, height: 18, bottom: 44 },
          { type: 'inside', yAxisIndex: 0 },
          { type: 'slider', yAxisIndex: 0, orient: 'vertical', width: 16, right: 8 },
        ],
        visualMap: {
          min: visualMin,
          max: visualMax,
          calculable: true,
          orient: 'horizontal',
          left: 'center',
          bottom: 0,
          inRange: {
            color: ['#2166ac', '#f7f7f7', '#b2182b'],
          },
          formatter: value => formatChartValue(value),
        },
        xAxis: {
          type: 'category',
          data: xLabels,
          splitArea: { show: true },
          axisLabel: {
            hideOverlap: true,
            rotate: xLabels.some(label => label.length > 8) ? 30 : 0,
          },
        },
        yAxis: {
          type: 'category',
          data: yLabels,
          splitArea: { show: true },
          axisLabel: { hideOverlap: true },
        },
        series: [
          {
            name: valueColumn,
            type: 'heatmap',
            data: heatmapData,
            progressive: 1000,
            emphasis: {
              itemStyle: {
                shadowBlur: 10,
                shadowColor: 'rgba(0, 0, 0, 0.28)',
              },
            },
          },
        ],
      };
    }

    const rows = resultRows
      .map(row => ({
        name: formatCellValue(row[dimensionColumn]),
        value: Number(row[valueColumn]),
      }))
      .filter(item => Number.isFinite(item.value));

    if (!rows.length) return null;

    if (chartType === 'pie') {
      return {
        tooltip: { trigger: 'item' },
        legend: { type: 'scroll', bottom: 0 },
        series: [
          {
            name: valueColumn,
            type: 'pie',
            radius: ['36%', '68%'],
            center: ['50%', '44%'],
            data: rows,
            label: { formatter: '{b}: {d}%' },
          },
        ],
      };
    }

    return {
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: chartType === 'bar' ? 'shadow' : 'cross' },
      },
      grid: { left: 56, right: 24, top: 36, bottom: 72 },
      dataZoom: [
        { type: 'inside' },
        { type: 'slider', height: 18, bottom: 28 },
      ],
      xAxis: {
        type: 'category',
        data: rows.map(item => item.name),
        axisLabel: { hideOverlap: true },
      },
      yAxis: { type: 'value', scale: true },
      series: [
        {
          name: valueColumn,
          type: chartType,
          data: rows.map(item => item.value),
          smooth: chartType === 'line',
          showSymbol: chartType !== 'line' || rows.length < 80,
          symbolSize: chartType === 'scatter' ? 8 : 4,
          lineStyle: { width: 2 },
        },
      ],
    };
  }, [chartType, dimensionColumn, heatmapYColumn, resultRows, valueColumn]);

  const fetchTables = useCallback(async () => {
    setSchemaLoading(true);
    try {
      const { data } = await request.get('/api/db/tables');
      const nextTables = data.tables || [];
      setTables(nextTables);
      setMaxLimit(data.max_limit || 500);

      if (nextTables.length && !selectedTableName) {
        setSelectedTableName(nextTables[0].name);
        setSqlSeed(prev => ({ id: prev.id + 1, sql: nextTables[0].sample_sql }));
      }
    } catch (error) {
      message.error(getErrorMessage(error, '加载数据表失败'));
    } finally {
      setSchemaLoading(false);
    }
  }, [selectedTableName]);

  const fetchFavorites = useCallback(async () => {
    setFavoritesLoading(true);
    try {
      const { data } = await request.get('/api/db/favorites');
      setFavorites(data || []);
    } catch (error) {
      message.error(getErrorMessage(error, '加载SQL收藏失败'));
    } finally {
      setFavoritesLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchTables();
    fetchFavorites();
  }, [fetchTables, fetchFavorites]);

  useEffect(() => {
    if (!resultColumns.length) {
      setDimensionColumn(undefined);
      setHeatmapYColumn(undefined);
      setValueColumn(undefined);
      return;
    }

    const firstNumericColumn = numericColumns[0];
    const dimensionColumns = resultColumns.filter(column => !numericColumns.includes(column));
    const firstDimensionColumn = dimensionColumns[0] || resultColumns[0];
    const secondDimensionColumn = dimensionColumns.find(column => column !== firstDimensionColumn)
      || resultColumns.find(column => column !== firstDimensionColumn && column !== firstNumericColumn)
      || resultColumns.find(column => column !== firstDimensionColumn)
      || firstDimensionColumn;
    setDimensionColumn(firstDimensionColumn);
    setHeatmapYColumn(secondDimensionColumn);
    setValueColumn(firstNumericColumn);
  }, [numericColumns, resultColumns]);

  const handleTableSelect = (table) => {
    setSelectedTableName(table.name);
    setSelectedFavoriteId(undefined);
    setSqlSeed(prev => ({ id: prev.id + 1, sql: table.sample_sql }));
  };

  const handleFavoriteClear = useCallback(() => {
    setSelectedFavoriteId(undefined);
  }, []);

  const handleFavoriteSelect = useCallback((favoriteId) => {
    const favorite = favorites.find(item => item.id === favoriteId);
    if (!favorite) return;

    setSelectedFavoriteId(favorite.id);
    setSqlSeed(prev => ({ id: prev.id + 1, sql: favorite.sql }));
    if (favorite.engine) {
      setQueryEngine(favorite.engine);
    }
  }, [favorites]);

  const handleOpenSaveFavorite = useCallback((sqlText) => {
    const trimmedSql = sqlText.trim();
    if (!trimmedSql) {
      message.warning('请先输入要收藏的SQL');
      return;
    }

    setPendingFavoriteSql(trimmedSql);
    setFavoriteName(selectedFavorite?.name || '');
    setFavoriteModalOpen(true);
  }, [selectedFavorite]);

  const handleSaveFavorite = async () => {
    const trimmedName = favoriteName.trim();
    if (!trimmedName) {
      message.warning('请给这个SQL起个名字');
      return;
    }
    if (!pendingFavoriteSql.trim()) {
      message.warning('SQL内容不能为空');
      return;
    }

    setFavoriteSaving(true);
    try {
      const { data } = await request.post('/api/db/favorites', {
        name: trimmedName,
        sql: pendingFavoriteSql.trim(),
        engine: queryEngine,
      });
      setFavorites(prev => {
        const withoutCurrent = prev.filter(item => item.id !== data.id);
        return [data, ...withoutCurrent];
      });
      setSelectedFavoriteId(data.id);
      setFavoriteModalOpen(false);
      setFavoriteName('');
      setPendingFavoriteSql('');
      message.success('SQL已加入收藏夹');
    } catch (error) {
      message.error(getErrorMessage(error, '保存SQL收藏失败'));
    } finally {
      setFavoriteSaving(false);
    }
  };

  const handleDeleteFavorite = useCallback(async () => {
    if (!selectedFavoriteId) return;

    setFavoriteDeleting(true);
    try {
      await request.delete(`/api/db/favorites/${selectedFavoriteId}`);
      setFavorites(prev => prev.filter(item => item.id !== selectedFavoriteId));
      setSelectedFavoriteId(undefined);
      message.success('SQL收藏已删除');
    } catch (error) {
      message.error(getErrorMessage(error, '删除SQL收藏失败'));
    } finally {
      setFavoriteDeleting(false);
    }
  }, [selectedFavoriteId]);

  const runQuery = useCallback(async (sqlText) => {
    const trimmedSql = sqlText.trim();
    if (!trimmedSql) {
      message.warning('请先输入SQL查询语句');
      return;
    }

    setQueryLoading(true);
    try {
      const { data } = await request.post('/api/db/query', { sql: trimmedSql, engine: queryEngine });
      setResult(data);
      setViewMode('table');
      if (data.limit_applied) {
        message.info(`未检测到LIMIT，后端已自动限制为${data.max_limit || maxLimit}行`);
      } else if ((data.row_count || 0) >= (data.max_limit || maxLimit)) {
        message.info(`结果已按最大${data.max_limit || maxLimit}行返回`);
      }
    } catch (error) {
      message.error(getErrorMessage(error, '查询失败'));
    } finally {
      setQueryLoading(false);
    }
  }, [maxLimit, queryEngine]);

  return (
    <>
      <div className="db-manager-page">
      <Card
        className="db-sidebar"
        title={(
          <Space>
            <DatabaseOutlined />
            <span>可查询表</span>
            <Tag>{tables.length}</Tag>
          </Space>
        )}
        extra={(
          <Tooltip title="刷新表结构">
            <Button icon={<ReloadOutlined />} size="small" onClick={fetchTables} loading={schemaLoading} />
          </Tooltip>
        )}
      >
        <Input
          allowClear
          prefix={<SearchOutlined />}
          placeholder="搜索表名或字段"
          value={tableSearch}
          onChange={event => setTableSearch(event.target.value)}
        />

        <List
          className="db-table-list"
          loading={schemaLoading}
          dataSource={filteredTables}
          locale={{ emptyText: <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="没有可查询表" /> }}
          renderItem={table => (
            <List.Item
              className={table.name === selectedTableName ? 'db-table-item active' : 'db-table-item'}
              onClick={() => handleTableSelect(table)}
            >
              <Space direction="vertical" size={4} className="db-table-item-body">
                <Space className="db-table-item-title">
                  <Text strong>{table.name}</Text>
                  <Tag color={table.source === 'duckdb' ? 'purple' : 'default'}>
                    {table.source === 'duckdb' ? 'DuckDB' : 'SQLite'}
                  </Tag>
                  <Tag color="blue">{table.column_count}</Tag>
                </Space>
                <Text type="secondary" className="db-table-fields">
                  {table.columns.slice(0, 5).map(column => column.name).join(', ')}
                  {table.columns.length > 5 ? ' ...' : ''}
                </Text>
              </Space>
            </List.Item>
          )}
        />

        {selectedTable && (
          <div className="db-column-panel">
            <Space size={6}>
              <Text strong>{selectedTable.name}</Text>
              <Tag color={selectedTable.source === 'duckdb' ? 'purple' : 'default'}>
                {selectedTable.source === 'duckdb' ? 'DuckDB' : 'SQLite'}
              </Tag>
            </Space>
            <div className="db-column-tags">
              {selectedTable.columns.map(column => (
                <Tooltip title={column.type || 'unknown'} key={column.name}>
                  <Tag color={column.primary_key ? 'gold' : undefined}>{column.name}</Tag>
                </Tooltip>
              ))}
            </div>
          </div>
        )}
      </Card>

      <div className="db-workbench">
        <SqlEditorCard
          favoriteDeleting={favoriteDeleting}
          favoriteOptions={favoriteOptions}
          favoritesLoading={favoritesLoading}
          maxLimit={maxLimit}
          queryEngine={queryEngine}
          queryLoading={queryLoading}
          selectedFavoriteId={selectedFavoriteId}
          sqlSeed={sqlSeed}
          suggestionSource={suggestionSource}
          onDeleteFavorite={handleDeleteFavorite}
          onEngineChange={setQueryEngine}
          onFavoriteClear={handleFavoriteClear}
          onFavoriteSelect={handleFavoriteSelect}
          onOpenSaveFavorite={handleOpenSaveFavorite}
          onRun={runQuery}
        />

        <Card
          className="db-result-card"
          title={(
            <Space>
              <TableOutlined />
              <Title level={5} style={{ margin: 0 }}>查询结果</Title>
              {result && <Tag>{result.row_count} 行</Tag>}
              {result && (
                <Tag color={result.engine === 'duckdb' ? 'purple' : 'default'}>
                  {result.engine === 'duckdb' ? 'DuckDB' : 'SQLite'}
                </Tag>
              )}
              {result?.timings?.request_total_ms !== undefined && (
                <Tag color="blue">{formatTiming(result.timings.request_total_ms)}</Tag>
              )}
            </Space>
          )}
          extra={(
            <Segmented
              value={viewMode}
              onChange={setViewMode}
              options={[
                { label: '表格', value: 'table', icon: <TableOutlined /> },
                { label: '图表', value: 'chart', icon: <BarChartOutlined /> },
              ]}
            />
          )}
        >
          {!!timingItems.length && (
            <div className="db-timing-row">
              {timingItems.map(item => (
                <Tag key={item.label}>{item.label}: {formatTiming(item.value)}</Tag>
              ))}
            </div>
          )}

          {!result ? (
            <Empty description="执行查询后在这里查看结果" />
          ) : viewMode === 'table' ? (
            <div className="db-result-table-wrap">
              <Table
                className="db-result-table"
                size="small"
                rowKey="__rowIndex"
                columns={tableColumns}
                dataSource={tableDataSource}
                scroll={{ x: Math.max(900, resultColumns.length * 168), y: 420 }}
                pagination={{
                  defaultPageSize: 50,
                  showSizeChanger: true,
                  pageSizeOptions: [20, 50, 100, 200, 500],
                  showTotal: total => `共 ${total} 行`,
                }}
              />
            </div>
          ) : (
            <div className="db-chart-view">
              <Space wrap className="db-chart-controls">
                <Select
                  value={chartType}
                  onChange={setChartType}
                  options={chartTypeOptions}
                  optionRender={option => (
                    <Space>
                      {option.data.icon}
                      {option.data.label}
                    </Space>
                  )}
                  style={{ width: 132 }}
                />
                <Select
                  placeholder={chartType === 'heatmap' ? 'X轴列' : '维度列'}
                  value={dimensionColumn}
                  onChange={setDimensionColumn}
                  options={resultColumns.map(column => ({ label: column, value: column }))}
                  style={{ width: 180 }}
                />
                {chartType === 'heatmap' && (
                  <Select
                    placeholder="Y轴列"
                    value={heatmapYColumn}
                    onChange={setHeatmapYColumn}
                    options={resultColumns.map(column => ({ label: column, value: column }))}
                    style={{ width: 180 }}
                  />
                )}
                <Select
                  placeholder="数据列"
                  value={valueColumn}
                  onChange={setValueColumn}
                  options={numericColumns.map(column => ({ label: column, value: column }))}
                  style={{ width: 180 }}
                />
              </Space>

              {!numericColumns.length ? (
                <Empty description="结果中没有可用于绘图的数值列" />
              ) : !chartOption ? (
                <Empty description={chartType === 'heatmap' ? '请选择X轴列、Y轴列和数据列' : '请选择维度列和数据列'} />
              ) : (
                <ReactECharts option={chartOption} style={{ height: 460 }} notMerge />
              )}
            </div>
          )}
        </Card>
      </div>
      </div>

      <Modal
        title="收藏当前SQL"
        open={favoriteModalOpen}
        okText="保存"
        cancelText="取消"
        confirmLoading={favoriteSaving}
        onOk={handleSaveFavorite}
        onCancel={() => {
          setFavoriteModalOpen(false);
          setFavoriteName('');
          setPendingFavoriteSql('');
        }}
      >
        <Space direction="vertical" size={12} className="db-favorite-modal">
          <Input
            autoFocus
            maxLength={120}
            showCount
            placeholder="比如：创新100每日成分统计"
            value={favoriteName}
            onChange={event => setFavoriteName(event.target.value)}
            onPressEnter={handleSaveFavorite}
          />
          <Text type="secondary">同名收藏会自动更新为当前SQL。</Text>
        </Space>
      </Modal>
    </>
  );
};

export default DatabaseManager;
