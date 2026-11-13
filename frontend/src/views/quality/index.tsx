import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/quality'
const columns = [
  "规则编号",
  "表名称",
  "校验字段",
  "校验类型",
  "阈值上界",
  "阈值下界",
  "阻断级别",
  "最近校验",
  "阈值版本",
  "越界区间",
  "确认状态",
  "阈值生效结论"
]
const actions = [
  "启动校验",
  "暂停校验",
  "归档规则"
]
const versionColumns = [
  "版本编号",
  "阈值上界",
  "阈值下界",
  "阻断级别",
  "生效时间",
  "复核状态",
  "完整度",
  "覆盖说明"
]

type Row = Record<string, string | number | null>

export default function QualityView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [noticeMessage, setNoticeMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [selectedRule, setSelectedRule] = useState<Row | null>(null)
  const [versions, setVersions] = useState<Row[]>([])
  const [thresholdForm, setThresholdForm] = useState<Record<string, string>>({
    阈值上界: '',
    阈值下界: '',
    阻断级别: '阻断',
    生效时间: '',
  })
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const stats = useMemo(() => ([
    { label: '质量规则总数', value: total },
    { label: '运行中规则', value: rows.filter((row) => row.status === '运行中').length },
    { label: '待复核版本', value: versions.filter((row) => row.复核状态 === '待复核').length },
  ]), [rows, total, versions])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('质量规则列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量探针列表读取失败')
    }
  }, [filters])

  const loadVersions = useCallback(async (rule: Row) => {
    try {
      const response = await request(`${ENDPOINT}/${rule.id}/thresholds`)
      if (!response.ok) {
        throw new Error('阈值版本列表读取失败')
      }
      const payload = await response.json()
      setVersions(payload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值版本列表读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ values: { action } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '质量探针动作未生效，请稍后重试')
      }
      setNoticeMessage(payload.message ?? '质量探针操作完成')
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '质量探针操作失败')
    }
  }

  function selectRule(row: Row) {
    setSelectedRule(row)
    setNoticeMessage('')
    setErrorMessage('')
    void loadVersions(row)
  }

  async function submitThreshold() {
    if (!selectedRule) {
      setErrorMessage('请先选择一条质量规则')
      return
    }
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${selectedRule.id}/thresholds`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            阈值上界: thresholdForm.阈值上界 === '' ? '' : Number(thresholdForm.阈值上界),
            阈值下界: thresholdForm.阈值下界 === '' ? '' : Number(thresholdForm.阈值下界),
            阻断级别: thresholdForm.阻断级别,
            生效时间: thresholdForm.生效时间,
          },
        }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '阈值版本提交失败')
      }
      setNoticeMessage(payload.message ?? '阈值版本已提交')
      await loadVersions(selectedRule)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值版本提交失败')
    }
  }

  async function reviewThreshold(version: Row, approve: boolean) {
    if (!selectedRule) {
      return
    }
    setErrorMessage('')
    setNoticeMessage('')
    try {
      const response = await request(`${ENDPOINT}/${selectedRule.id}/thresholds/${version.id}/review`, {
        method: 'POST',
        body: JSON.stringify({ values: { approve, reviewer: '值班管理员' } }),
      })
      const payload = await response.json()
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message ?? '阈值版本复核失败')
      }
      setNoticeMessage(payload.message ?? '阈值版本复核完成')
      await loadVersions(selectedRule)
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '阈值版本复核失败')
    }
  }

  return (
    <section className="page" data-module="quality">
      <header className="page-head">
        <div>
          <h2>质量探针管理</h2>
          <p className="page-desc">维护质量规则的校验类型、阈值设置与阻断策略，为数据质量保障提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('质量规则登记入口尚未接入审批流')}>
            登记质量规则
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出质量探针清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {column === '确认状态' ? (
                    <span className={`tag ${row[column] === '未确认' ? 'warn' : 'ok'}`}>{row[column] ?? '—'}</span>
                  ) : (
                    row[column] ?? '—'
                  )}
                </td>
              ))}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                <button className="link" type="button" onClick={() => selectRule(row)}>
                  阈值版本
                </button>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无质量探针数据，可先登记质量规则</td>
            </tr>
          )}
        </tbody>
      </table>

      {selectedRule && (
        <section className="panel">
          <h3>阈值版本治理：{selectedRule.规则编号}</h3>
          <p className="page-desc">
            新阈值先进入准入复核，复核通过并到达生效时间后只影响之后采集的读数；历史读数按原版本留档，被覆盖的版本保留供核对。
          </p>
          <table className="data-table">
            <thead>
              <tr>
                {versionColumns.map((column) => <th key={column}>{column}</th>)}
                <th>复核操作</th>
              </tr>
            </thead>
            <tbody>
              {versions.map((version) => (
                <tr key={String(version.id)}>
                  {versionColumns.map((column) => <td key={column}>{version[column] || '—'}</td>)}
                  <td className="row-actions">
                    {version.复核状态 === '待复核' ? (
                      <>
                        <button className="link" type="button" onClick={() => void reviewThreshold(version, true)}>复核通过</button>
                        <button className="link" type="button" onClick={() => void reviewThreshold(version, false)}>复核驳回</button>
                      </>
                    ) : (
                      <span>{version.status ?? '—'}</span>
                    )}
                  </td>
                </tr>
              ))}
              {versions.length === 0 && (
                <tr>
                  <td className="empty-state" colSpan={versionColumns.length + 1}>该规则还没有阈值版本</td>
                </tr>
              )}
            </tbody>
          </table>
          <form
            className="filter-bar"
            onSubmit={(event) => { event.preventDefault(); void submitThreshold() }}
          >
            <label className="filter-item">
              <span>阈值上界</span>
              <input
                value={thresholdForm.阈值上界}
                placeholder="如 90"
                onChange={(event) => setThresholdForm((prev) => ({ ...prev, 阈值上界: event.target.value }))}
              />
            </label>
            <label className="filter-item">
              <span>阈值下界</span>
              <input
                value={thresholdForm.阈值下界}
                placeholder="如 10"
                onChange={(event) => setThresholdForm((prev) => ({ ...prev, 阈值下界: event.target.value }))}
              />
            </label>
            <label className="filter-item">
              <span>阻断级别</span>
              <input
                value={thresholdForm.阻断级别}
                placeholder="阻断 / 告警"
                onChange={(event) => setThresholdForm((prev) => ({ ...prev, 阻断级别: event.target.value }))}
              />
            </label>
            <label className="filter-item">
              <span>生效时间</span>
              <input
                type="datetime-local"
                value={thresholdForm.生效时间}
                onChange={(event) => setThresholdForm((prev) => ({ ...prev, 生效时间: event.target.value }))}
              />
            </label>
            <button className="btn primary" type="submit">提交阈值版本（进入准入复核）</button>
          </form>
        </section>
      )}

      <footer className="page-foot">
        <span>共 {total} 条质量探针记录</span>
        {noticeMessage && <span className="notice-text">{noticeMessage}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
