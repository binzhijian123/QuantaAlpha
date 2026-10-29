import React from 'react';
import { Sparkles, Database, BarChart3, Settings as SettingsIcon } from 'lucide-react';
import { useTaskContext } from '@/context/TaskContext';

export type PageId = 'home' | 'library' | 'backtest' | 'settings' | 'mining_dashboard';

interface LayoutProps {
  children: React.ReactNode;
  currentPage: PageId;
  onNavigate: (page: PageId) => void;
  showNavigation?: boolean;
}

export const Layout: React.FC<LayoutProps> = ({
  children,
  currentPage,
  onNavigate,
  showNavigation = true,
}) => {
  const { miningTask } = useTaskContext();

  // Helper to determine where 'Factor Mining' nav item should go
  const handleNavClick = (itemId: PageId) => {
    if (itemId === 'home') {
      // If there is an active mining task (running, completed, or failed), go to dashboard
      // Only go to home welcome screen if task is null or idle
      if (miningTask && miningTask.status !== 'idle') {
        onNavigate('mining_dashboard');
      } else {
        onNavigate('home');
      }
    } else {
      onNavigate(itemId);
    }
  };

  const navItems = [
    { id: 'home' as const, label: '因子挖掘', icon: Sparkles },
    { id: 'library' as const, label: '因子库', icon: Database },
    { id: 'backtest' as const, label: '回测', icon: BarChart3 },
    { id: 'settings' as const, label: '设置', icon: SettingsIcon },
  ];

  return (
    <div className="min-h-screen bg-background gradient-mesh">
      {/* Header */}
      <header className="fixed top-0 left-0 right-0 z-40 glass-strong border-b border-border/50">
        <div className="container mx-auto px-3 py-3 sm:px-6 sm:py-4">
          <div className="flex items-center justify-between">
            <div 
              className="flex min-w-0 items-center gap-2 cursor-pointer hover:opacity-80 transition-opacity sm:gap-3"
              onClick={() => onNavigate('home')}
            >
              <div className="relative">
                <div className="relative flex h-10 w-10 items-center justify-center rounded-lg bg-gradient-to-br from-primary to-purple-600 sm:h-12 sm:w-12 sm:rounded-xl">
                  <Sparkles className="h-6 w-6 text-white" />
                </div>
              </div>
              <div>
                <h1 className="text-base font-bold sm:text-xl">QuantaAlpha</h1>
                <p className="hidden text-xs text-muted-foreground sm:block">智能因子挖掘平台</p>
              </div>
            </div>

            {/* Navigation */}
            <div className="flex items-center gap-4">
              {showNavigation && (
                <nav className="flex items-center gap-0.5 sm:gap-2">
                  {navItems.map((item) => {
                    const Icon = item.icon;
                    // Highlight 'Factor Mining' if we are on dashboard OR home
                    const isActive = currentPage === item.id || (item.id === 'home' && currentPage === 'mining_dashboard');
                    return (
                      <button
                        key={item.id}
                        onClick={() => handleNavClick(item.id)}
                        aria-label={item.label}
                        title={item.label}
                        className={`flex h-10 w-10 items-center justify-center gap-2 rounded-lg transition-all md:w-auto md:px-4 ${
                          isActive
                            ? 'bg-primary text-primary-foreground'
                            : 'text-muted-foreground hover:text-foreground hover:bg-secondary/50'
                        }`}
                      >
                        <Icon className="h-4 w-4" />
                        <span className="hidden text-sm font-medium md:inline">{item.label}</span>
                      </button>
                    );
                  })}
                </nav>
              )}
            </div>
          </div>
        </div>
      </header>

      {/* Main Content — pb-48 ensures content is never hidden behind the fixed ChatInput */}
      <main className="pt-20 pb-48 sm:pt-24">
        <div className="container mx-auto px-3 sm:px-6">{children}</div>
      </main>
    </div>
  );
};
