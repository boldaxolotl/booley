/* Process-scoped Linux syscall boundary fixture. Never install system-wide. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <time.h>
#include <unistd.h>

static __thread int in_hook;
static unsigned sequence;

static int absolute_at(int fd, const char *path, char *out) {
    char base[PATH_MAX], link[64];
    if (!path || strchr(path, '\n') || strchr(path, '\t')) return 0;
    if (path[0] == '/') return snprintf(out, PATH_MAX, "%s", path) < PATH_MAX;
    if (fd == AT_FDCWD) {
        if (!getcwd(base, sizeof(base))) return 0;
    } else {
        snprintf(link, sizeof(link), "/proc/self/fd/%d", fd);
        ssize_t n = readlink(link, base, sizeof(base)-1);
        if (n < 0) return 0;
        base[n] = 0;
    }
    return snprintf(out, PATH_MAX, "%s/%s", base, path) < PATH_MAX;
}

static int selected(const char *op, const char *path) {
    const char *root=getenv("QA_FAULT_OWNED"), *suffix=getenv("QA_FAULT_SUFFIX");
    const char *operation=getenv("QA_FAULT_OPERATION");
    if (!root || !suffix || !operation || strcmp(op,operation)) return 0;
    size_t n=strlen(root), m=strlen(path), s=strlen(suffix);
    return m>n && !strncmp(root,path,n) && path[n]=='/' && m>=s &&
           !strcmp(path+m-s,suffix) && !strstr(path,"/../") && !strstr(path,"/./");
}

static int decision(const char *op, const char *src, const char *dst) {
    const char *control=getenv("QA_FAULT_CONTROL");
    char event[PATH_MAX], reply[PATH_MAX + 6], consumed[PATH_MAX];
    if (!control || !selected(op,dst)) return 0;
    snprintf(consumed,sizeof(consumed),"%s/consumed",control);
    if (access(consumed,F_OK)==0) return 0;
    snprintf(event,sizeof(event),"%s/event-%ld-%u",control,(long)getpid(),sequence++);
    snprintf(reply,sizeof(reply),"%s.reply",event);
    int fd=open(event,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0600);
    if(fd<0) return 0;
    dprintf(fd,"%s\t%s\t%s\n",op,src,dst); fsync(fd); close(fd);
    const struct timespec delay={0,10000000};
    char response='F'; /* a lost controller fails closed, never hangs a producer */
    for(int i=0;i<2000;i++) {
        fd=open(reply,O_RDONLY|O_NOFOLLOW);
        if(fd>=0) { if(read(fd,&response,1)!=1) response='F'; close(fd); break; }
        nanosleep(&delay,NULL);
    }
    if(response=='P') return 0;
    fd=open(consumed,O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0600);
    if(fd<0) return 0;
    dprintf(fd,"%s\n",event); fsync(fd); close(fd);
    errno=EIO; return 1;
}

static int intercept(const char *op,int sfd,const char *src,int dfd,const char *dst) {
    char source[PATH_MAX]="",destination[PATH_MAX];
    if(in_hook || !absolute_at(dfd,dst,destination)) return 0;
    if(src && !absolute_at(sfd,src,source)) return 0;
    in_hook=1;
    int fail=decision(op,source,destination);
    in_hook=0;
    if(fail) errno=EIO;
    return fail;
}

int link(const char *a,const char *b) {
    if(intercept("link",AT_FDCWD,a,AT_FDCWD,b)) return -1;
    return syscall(SYS_link,a,b);
}
int linkat(int af,const char *a,int bf,const char *b,int flags) {
    if(intercept("link",af,a,bf,b)) return -1;
    return syscall(SYS_linkat,af,a,bf,b,flags);
}
int rename(const char *a,const char *b) {
    if(intercept("rename",AT_FDCWD,a,AT_FDCWD,b)) return -1;
    return syscall(SYS_rename,a,b);
}
int renameat(int af,const char *a,int bf,const char *b) {
    if(intercept("rename",af,a,bf,b)) return -1;
    return syscall(SYS_renameat,af,a,bf,b);
}
int renameat2(int af,const char *a,int bf,const char *b,unsigned flags) {
    if(intercept("rename",af,a,bf,b)) return -1;
    return syscall(SYS_renameat2,af,a,bf,b,flags);
}
int unlink(const char *path) {
    if(intercept("unlink",AT_FDCWD,NULL,AT_FDCWD,path)) return -1;
    return syscall(SYS_unlink,path);
}
int rmdir(const char *path) {
    if(intercept("unlink",AT_FDCWD,NULL,AT_FDCWD,path)) return -1;
    return syscall(SYS_rmdir,path);
}
int unlinkat(int fd,const char *path,int flags) {
    if(intercept("unlink",AT_FDCWD,NULL,fd,path)) return -1;
    return syscall(SYS_unlinkat,fd,path,flags);
}

static const char *redirect(const char *path,char *const argv[]) {
    const char *exact=getenv("QA_CLAUDE_EXECUTABLE"), *shim=getenv("QA_CLAUDE_SHIM");
    if(!exact || !shim || strcmp(exact,path)) return path;
    for(int i=0;argv && argv[i] && i<256;i++)
        if(!strcmp(argv[i],"stream-json")) return shim;
    return path; /* real SDK version probe is never intercepted */
}
int execve(const char *path,char *const argv[],char *const env[]) {
    return syscall(SYS_execve,redirect(path,argv),argv,env);
}
extern char **environ;
int execv(const char *path,char *const argv[]) { return execve(path,argv,environ); }
int posix_spawn(pid_t *pid,const char *path,const posix_spawn_file_actions_t *actions,
                const posix_spawnattr_t *attr,char *const argv[],char *const env[]) {
    typedef int (*fn)(pid_t*,const char*,const posix_spawn_file_actions_t*,
                      const posix_spawnattr_t*,char *const[],char *const[]);
    fn real=(fn)dlsym(RTLD_NEXT,"posix_spawn");
    return real(pid,redirect(path,argv),actions,attr,argv,env);
}
int posix_spawnp(pid_t *pid,const char *path,const posix_spawn_file_actions_t *actions,
                 const posix_spawnattr_t *attr,char *const argv[],char *const env[]) {
    typedef int (*fn)(pid_t*,const char*,const posix_spawn_file_actions_t*,
                      const posix_spawnattr_t*,char *const[],char *const[]);
    fn real=(fn)dlsym(RTLD_NEXT,"posix_spawnp");
    return real(pid,redirect(path,argv),actions,attr,argv,env);
}
