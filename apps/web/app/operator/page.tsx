import { OperatorDashboard } from "@/components/operator-dashboard";
import { AccountSessionProvider } from "@/components/account-session";

export default function OperatorPage() {
  return <div className="minimal-shell"><AccountSessionProvider><OperatorDashboard /></AccountSessionProvider></div>;
}
