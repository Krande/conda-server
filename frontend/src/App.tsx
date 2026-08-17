import { Routes, Route, Navigate } from "react-router-dom";
import Layout from "./components/Layout";
import Home from "./pages/Home";
import Channels from "./pages/Channels";
import ChannelDetail from "./pages/ChannelDetail";
import PackageDetail from "./pages/PackageDetail";
import Tokens from "./pages/Tokens";
import Profile from "./pages/Profile";
import About from "./pages/About";
import Admin from "./pages/Admin";
import AdminAudit from "./pages/AdminAudit";
import NotFound from "./pages/NotFound";

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Home />} />
        <Route path="channels" element={<Channels />} />
        <Route path="channels/:channel" element={<ChannelDetail />} />
        <Route
          path="channels/:channel/packages/:name"
          element={<PackageDetail />}
        />
        <Route path="profile" element={<Profile />} />
        <Route path="tokens" element={<Tokens />} />
        <Route path="about" element={<About />} />
        <Route path="admin" element={<Admin />} />
        <Route path="admin/audit" element={<AdminAudit />} />
        <Route path="login" element={<Navigate to="/api/auth/login" replace />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
