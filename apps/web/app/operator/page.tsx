import { OperatorDashboard } from "@/components/operator-dashboard";
import { AccountSessionProvider } from "@/components/account-session";

export default function OperatorPage() {
  return <div className="min-h-screen overflow-x-hidden bg-[#080b10]"><AccountSessionProvider><OperatorDashboard /></AccountSessionProvider></div>;
}
