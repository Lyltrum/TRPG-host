import { Routes, Route, Navigate } from 'react-router-dom';
import { lazy, Suspense, useEffect, useState } from 'react';
import PhoneLayout from '@/shared/layouts/PhoneLayout';
import { getAuthToken } from '@/services/api-client';
import { fetchMe } from '@/services/auth';
import { useAuthStore } from '@/stores/auth-store';

const LoginPage = lazy(() => import('@/routes/auth/LoginPage'));
const RegisterPage = lazy(() => import('@/routes/auth/RegisterPage'));
const HomePage = lazy(() => import('@/routes/home/HomePage'));
const JoinRoomPage = lazy(() => import('@/routes/join/JoinRoomPage'));
const InviteLandingPage = lazy(() => import('@/routes/join/InviteLandingPage'));
const CreateRoomPage = lazy(() => import('@/routes/create/CreateRoomPage'));
const GameSelectionPage = lazy(() => import('@/routes/games/GameSelectionPage'));
const ScenarioSelectionPage = lazy(() => import('@/routes/games/trpg/ScenarioSelectionPage'));
const StoryPage = lazy(() => import('@/routes/games/trpg/StoryPage'));
const CharacterPage = lazy(() => import('@/routes/games/trpg/CharacterPage'));
const LobbyPage = lazy(() => import('@/routes/lobby/LobbyPage'));
const CharacterReadyPage = lazy(() => import('@/routes/character-ready/CharacterReadyPage'));
const RoomPage = lazy(() => import('@/routes/games/trpg/RoomPage'));
const MyRoomsPage = lazy(() => import('@/routes/my-rooms/MyRoomsPage'));
const MyCharactersPage = lazy(() => import('@/routes/my-characters/MyCharactersPage'));
const MyCharacterDetailPage = lazy(
  () => import('@/routes/my-characters/MyCharacterDetailPage')
);
const ReviewPage = lazy(() => import('@/routes/review/ReviewPage'));
const ProfilePage = lazy(() => import('@/routes/profile/ProfilePage'));
const MyModulesPage = lazy(() => import('@/routes/modules/MyModulesPage'));
const ImportModulePage = lazy(() => import('@/routes/modules/ImportModulePage'));
const ImportJobPage = lazy(() => import('@/routes/modules/ImportJobPage'));

function LoadingFallback() {
  return (
    <div className="flex items-center justify-center min-h-[60vh] text-text-muted text-sm">
      加载中…
    </div>
  );
}

/**
 * 顶层路由（issue #66：前端原型接入 trpg-frontend）。路由结构照搬前端
 * 原型，业务数据全部走真实后端 + trpg-sdk，不再有 mock 模式。
 */
function App() {
  const login = useAuthStore((s) => s.login);
  // auth-store 只存在内存里（不持久化），但登录 token 一直躺在 localStorage。
  // 页面刷新/HMR 全量重载后 auth-store 会被清空，若不在这里用 token 换回身份，
  // 所有需要登录守卫的页面（如 /home）会误判成"未登录"直接弹回登录页。
  const [restoring, setRestoring] = useState(!!getAuthToken());

  useEffect(() => {
    const token = getAuthToken();
    if (!token) return;
    fetchMe().then((me) => {
      if (me) login(token, me.userId, me.nickname);
      setRestoring(false);
    });
  }, [login]);

  if (restoring) return <LoadingFallback />;

  return (
    <PhoneLayout>
      <Suspense fallback={<LoadingFallback />}>
        <Routes>
          <Route path="/" element={<Navigate to="/auth/login" replace />} />

          {/* /auth/* —— 登录和注册是身份验证下两个平级的入口方式（互斥的
              替代动作，不是谁从属谁），所以是同一个前缀下的两个兄弟路径，
              不是互相嵌套。 */}
          {/* 受邀链接落地页。**放在登录之外**：朋友点开链接时多半还没有身份，
              这一屏自己会给他建一个（只问昵称），不该先把他弹去登录页。 */}
          <Route path="/join/:roomCode" element={<InviteLandingPage />} />

          <Route path="/auth/login" element={<LoginPage />} />
          <Route path="/auth/register" element={<RegisterPage />} />

          {/* /home/* —— 登录后的主页枢纽：从主页出发的每个子功能都挂在它下面，
              而不是各自平铺成顶层路径。 */}
          <Route path="/home" element={<HomePage />} />
          <Route path="/home/join" element={<JoinRoomPage />} />
          <Route path="/home/create" element={<CreateRoomPage />} />
          <Route path="/home/create/games" element={<GameSelectionPage />} />
          <Route
            path="/home/create/games/:gameId/scenarios/:systemId"
            element={<ScenarioSelectionPage />}
          />
          <Route path="/home/my-rooms" element={<MyRoomsPage />} />
          <Route path="/home/characters" element={<MyCharactersPage />} />
          <Route path="/home/characters/:templateId" element={<MyCharacterDetailPage />} />
          <Route path="/home/my-rooms/review/:roomCode" element={<ReviewPage />} />
          <Route path="/home/profile" element={<ProfilePage />} />
          {/* 模组导入（exec/29 第 5 步）。`/import` 必须排在 `/:jobId` 之前，
              否则会被当成 jobId="import"——跟后端路由那一处同一个坑。 */}
          <Route path="/home/modules" element={<MyModulesPage />} />
          <Route path="/home/modules/import" element={<ImportModulePage />} />
          <Route path="/home/modules/:jobId" element={<ImportJobPage />} />

          {/* /room/* —— 已经加入/创建了房间之后的整条游戏内流程：大厅→背景介绍→
              建卡→建卡准备→聊天室，都是同一个房间生命周期里的阶段。 */}
          <Route path="/room/lobby" element={<LobbyPage />} />
          <Route path="/room/story" element={<StoryPage />} />
          <Route path="/room/character" element={<CharacterPage />} />
          <Route path="/room/ready" element={<CharacterReadyPage />} />
          <Route path="/room/play" element={<RoomPage />} />
        </Routes>
      </Suspense>
    </PhoneLayout>
  );
}

export default App;
