import React from 'react';
import { Drawer, Space } from 'antd';
import './PageScaffold.css';

export const PageShell = ({ title, actions, children, className = '' }) => (
  <main className={`quant-page ${className}`.trim()}>
    <div className="quant-page__inner">
      {(title || actions) && (
        <header className="quant-page__header">
          <div className="quant-page__heading">
            {title && <h1>{title}</h1>}
          </div>
          {actions && <div className="quant-page__actions">{actions}</div>}
        </header>
      )}
      {children}
    </div>
  </main>
);

export const PageSection = ({ title, extra, children, className = '' }) => (
  <section className={`quant-section ${className}`.trim()}>
    {(title || extra) && (
      <div className="quant-section__header">
        {title && <h2>{title}</h2>}
        {extra && <div className="quant-section__extra">{extra}</div>}
      </div>
    )}
    <div className="quant-section__body">{children}</div>
  </section>
);

export const ResponsiveToolbar = ({ children, extra, className = '' }) => (
  <div className={`quant-toolbar ${className}`.trim()}>
    <div className="quant-toolbar__main">{children}</div>
    {extra && <div className="quant-toolbar__extra">{extra}</div>}
  </div>
);

export const MobileFilterDrawer = ({ open, title = '筛选', onClose, children, footer }) => (
  <Drawer
    title={title}
    open={open}
    onClose={onClose}
    placement="bottom"
    height="auto"
    className="quant-filter-drawer"
    styles={{
      body: { padding: 16 },
      footer: { padding: '10px 16px calc(10px + env(safe-area-inset-bottom))' },
    }}
    footer={footer ? <Space className="quant-filter-drawer__footer">{footer}</Space> : null}
    destroyOnClose={false}
  >
    {children}
  </Drawer>
);
