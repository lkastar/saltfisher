import { Link } from "react-router";

import { PageHero } from "../components/PageHero";

/** ponytail: placeholder until step 4 builds the real overview (KPI grid,
 *  task health table, recent hits/pushes, 14-day activity chart). */
export default function OverviewPage() {
  return (
    <>
      <PageHero
        eyebrow="SYSTEM OVERVIEW"
        ghost="SIGNAL"
        title={
          <>
            总览<span className="thin"> / 信号台</span>
          </>
        }
      />
      <p className="muted">
        总览建设中。监控任务的创建与管理暂时仍在
        <Link to="/monitors">监控任务</Link>页。
      </p>
    </>
  );
}
