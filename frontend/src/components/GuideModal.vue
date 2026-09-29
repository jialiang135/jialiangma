<script setup>
/**
 * 使用指南弹窗
 * ============
 *
 * 改造前它是自成一体的第 3 套弹窗实现：自己的 `.guide-overlay` / `.guide-dialog`，
 * 连 `fadeIn` / `scaleIn` 关键帧都在这个文件和全局样式表里各定义了一遍
 * （scoped 的 keyframes 不共享，浏览器里真的是两份）。
 *
 * 现在改用 `UiModal` —— 遮罩、Esc 关闭、锁滚动、焦点管理都是现成的，
 * 这里只负责内容。
 *
 * 文案也更新了：加了证据轨的说明（这是产品的核心差异点，值得在指南里讲），
 * 并且把「查看完整推理过程」改成实际的「查看推理过程」。
 */
import UiIcon from './ui/UiIcon.vue'
import UiModal from './ui/UiModal.vue'

defineProps({ show: Boolean })
const emit = defineEmits(['close'])
</script>

<template>
  <UiModal
    :open="show"
    size="lg"
    title="使用指南"
    @close="emit('close')"
  >
    <p class="lead">个人数字分身 · AI 面试助手</p>

    <section class="section">
      <h3 class="section-title">
        <UiIcon name="user" :size="15" />
        用户身份
      </h3>
      <div class="roles">
        <div class="role is-admin">
          <span class="badge-like">管理员</span>
          <span>可上传 / 删除 / 清空知识库文档，管理所有数据</span>
        </div>
        <div class="role is-user">
          <span class="badge-like">普通用户</span>
          <span>可提问对话，无法管理知识库</span>
        </div>
      </div>
      <p class="note">知识库内容由管理员统一维护，普通用户直接使用即可。</p>
    </section>

    <section class="section">
      <h3 class="section-title">
        <UiIcon name="zap" :size="15" />
        两步上手
      </h3>
      <ol class="steps">
        <li>
          <span class="step-num">1</span>
          <span>右上角输入用户名和密码，点「注册」（密码至少 8 位，含数字和字母），然后登录</span>
        </li>
        <li>
          <span class="step-num">2</span>
          <span>在「对话」页面输入问题，回车发送，等待回答</span>
        </li>
      </ol>
    </section>

    <section class="section">
      <h3 class="section-title">
        <UiIcon name="message" :size="15" />
        对话技巧
      </h3>
      <ul class="tips">
        <li><b>问具体：</b>「我做过哪些 Java 项目？」比「介绍你自己」更精准</li>
        <li><b>可追问：</b>回答后继续追问，系统会保留上下文</li>
        <li><b>看依据：</b>回答下方可展开「依据 N 个知识库片段」，看到这个答案是<strong>从哪些文档片段得出的</strong>，以及每段的相关度</li>
        <li><b>看推理：</b>「查看推理过程」里是检索与生成的完整步骤，以及模型的思考流</li>
        <li><b>朗读：</b>回答下方的「朗读」可用语音把答案读出来</li>
        <li><b>仅基于知识库：</b>系统只回答已上传文档中的内容，不会编造信息</li>
      </ul>
    </section>

    <section class="section">
      <h3 class="section-title">
        <UiIcon name="activity" :size="15" />
        手机端使用
      </h3>
      <p class="note">浏览器打开即可，界面会自动适配手机屏幕。左上角的菜单按钮打开导航。</p>
    </section>
  </UiModal>
</template>

<style scoped>
.lead {
  margin-bottom: var(--sp-5);
  font-size: var(--fs-sm);
  color: var(--c-text-3);
}

.section + .section {
  margin-top: var(--sp-6);
}

.section-title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-3);
  font-size: var(--fs-base);
  font-weight: var(--fw-semibold);
  color: var(--c-text);
}

.roles {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.role {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  padding: var(--sp-3);
  font-size: var(--fs-sm);
  border: 1px solid var(--c-border);
  border-radius: var(--r-md);
}
.role.is-admin {
  color: var(--c-accent-text);
  background: var(--c-accent-soft);
  border-color: var(--c-accent-border);
}
.role.is-user {
  color: var(--c-success-text);
  background: var(--c-success-soft);
  border-color: var(--c-success-border);
}
/* 标签样式：用 :deep 是因为 badge-like 是 render 出来的普通 span */
.role :deep(.badge-like) {
  flex-shrink: 0;
  padding: 2px var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  background: var(--c-surface);
  border-radius: var(--r-full);
}

.note {
  margin-top: var(--sp-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
}

.steps {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}
.steps li {
  display: flex;
  align-items: flex-start;
  gap: var(--sp-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
}
.step-num {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  font-size: var(--fs-xs);
  font-weight: var(--fw-semibold);
  color: var(--c-white);
  background: var(--c-accent);
  border-radius: 50%;
}

.tips {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.tips li {
  padding: var(--sp-2) var(--sp-3);
  font-size: var(--fs-sm);
  line-height: var(--lh-base);
  color: var(--c-text-2);
  background: var(--c-surface-2);
  border-radius: var(--r-md);
}
.tips b {
  color: var(--c-text);
}
</style>
