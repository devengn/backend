from unittest.mock import call, Mock

import pendulum
import pytest

from app.models.feed import FeedManager
from app.models.followed_first_story import FollowedFirstStoryManager
from app.models.media.enums import MediaStatus
from app.models.post.enums import PostStatus


@pytest.fixture
def post_with_expiration(post_manager, user_manager):
    user = user_manager.create_cognito_only_user('pbuid2', 'pbUname2')
    yield post_manager.add_post(user.id, 'pid2', text='t', lifetime_duration=pendulum.duration(hours=1))


@pytest.fixture
def post_with_media(post_manager, user_manager):
    user = user_manager.create_cognito_only_user('pbuid2', 'pbUname2')
    yield post_manager.add_post(user.id, 'pid2', media_uploads=[{'mediaId': 'mid'}], text='t')


@pytest.fixture
def post_with_media_completed(post_manager, user_manager, image_data_b64, mock_post_verification_api):
    user = user_manager.create_cognito_only_user('pbuid2', 'pbUname2')
    yield post_manager.add_post(
        user.id, 'pid2', media_uploads=[{'mediaId': 'mid', 'imageData': image_data_b64}], text='t',
    )


def test_restore_completed_text_only_post_with_expiration(post_manager, post_with_expiration, user_manager):
    post = post_with_expiration
    posted_by_user_id = post.item['postedByUserId']
    posted_by_user = user_manager.get_user(posted_by_user_id)

    # archive the post
    post.archive()
    assert post.item['postStatus'] == PostStatus.ARCHIVED

    # check our starting post count
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 0

    # mock out some calls to far-flung other managers
    post.followed_first_story_manager = Mock(FollowedFirstStoryManager({}))
    post.feed_manager = Mock(FeedManager({}))

    # restore the post
    post.restore()
    assert post.item['postStatus'] == PostStatus.COMPLETED

    # check the post straight from the db
    post.refresh_item()
    assert post.item['postStatus'] == PostStatus.COMPLETED

    # check our post count - should have incremented
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 1

    # check calls to mocked out managers
    post.item['mediaObjects'] = []
    assert post.followed_first_story_manager.mock_calls == [
        call.refresh_after_story_change(story_now=post.item),
    ]
    assert post.feed_manager.mock_calls == [
        call.add_post_to_followers_feeds(posted_by_user_id, post.item),
    ]


def test_restore_pending_media_post(post_manager, post_with_media, user_manager):
    post = post_with_media
    posted_by_user_id = post.item['postedByUserId']
    posted_by_user = user_manager.get_user(posted_by_user_id)

    # archive the post
    post.archive()
    assert post.item['postStatus'] == PostStatus.ARCHIVED
    assert len(post.item['mediaObjects']) == 1
    assert post.item['mediaObjects'][0]['mediaStatus'] == MediaStatus.ARCHIVED

    # check our starting post count
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 0

    # mock out some calls to far-flung other managers
    post.followed_first_story_manager = Mock(FollowedFirstStoryManager({}))
    post.feed_manager = Mock(FeedManager({}))

    # restore the post
    post.restore()
    assert post.item['postStatus'] == PostStatus.PENDING
    assert len(post.item['mediaObjects']) == 1
    assert post.item['mediaObjects'][0]['mediaStatus'] == MediaStatus.AWAITING_UPLOAD

    # check the DB again
    post.refresh_item()
    assert post.item['postStatus'] == PostStatus.PENDING
    post_media_items = list(post_manager.media_manager.dynamo.generate_by_post(post.id))
    assert len(post_media_items) == 1
    assert post_media_items[0]['mediaStatus'] == MediaStatus.AWAITING_UPLOAD

    # check our post count - should not have changed
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 0

    # check calls to mocked out managers
    assert post.followed_first_story_manager.mock_calls == []
    assert post.feed_manager.mock_calls == []


def test_restore_completed_media_post(post_manager, post_with_media_completed, user_manager):
    post = post_with_media_completed
    media = post_manager.media_manager.init_media(post.item['mediaObjects'][0])
    posted_by_user_id = post.item['postedByUserId']
    posted_by_user = user_manager.get_user(posted_by_user_id)

    # archive the post
    post.archive()
    assert post.item['postStatus'] == PostStatus.ARCHIVED
    assert len(post.item['mediaObjects']) == 1
    assert post.item['mediaObjects'][0]['mediaStatus'] == MediaStatus.ARCHIVED

    # check our starting post count
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 0

    # mock out some calls to far-flung other managers
    post.followed_first_story_manager = Mock(FollowedFirstStoryManager({}))
    post.feed_manager = Mock(FeedManager({}))

    # restore the post
    post.restore()
    assert post.item['postStatus'] == PostStatus.COMPLETED
    assert len(post.item['mediaObjects']) == 1
    assert post.item['mediaObjects'][0]['mediaStatus'] == MediaStatus.UPLOADED

    # check the DB again
    post.refresh_item()
    assert post.item['postStatus'] == PostStatus.COMPLETED
    media.refresh_item()
    assert media.item['mediaStatus'] == MediaStatus.UPLOADED

    # check our post count - should have incremented
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 1

    # check calls to mocked out managers
    post.item['mediaObjects'] = [media.item]
    assert post.followed_first_story_manager.mock_calls == []
    assert post.feed_manager.mock_calls == [
        call.add_post_to_followers_feeds(posted_by_user_id, post.item),
    ]


def test_restore_completed_post_in_album(album_manager, post_manager, post_with_media_completed, user_manager):
    post = post_with_media_completed
    media = post_manager.media_manager.init_media(post.item['mediaObjects'][0])
    posted_by_user = user_manager.get_user(post.item['postedByUserId'])
    album = album_manager.add_album(posted_by_user.id, 'aid', 'album name')
    post.set_album(album.id)

    # archive the post
    post.archive()
    assert post.item['postStatus'] == PostStatus.ARCHIVED

    # check our starting post count
    album.refresh_item()
    assert album.item.get('postCount', 0) == 0
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 0

    # mock out some calls to far-flung other managers
    post.album_manager.update_album_art_if_needed = Mock()
    post.followed_first_story_manager = Mock(FollowedFirstStoryManager({}))
    post.feed_manager = Mock(FeedManager({}))

    # restore the post
    post.restore()
    assert post.item['postStatus'] == PostStatus.COMPLETED

    # check the post straight from the db
    post.refresh_item()
    assert post.item['postStatus'] == PostStatus.COMPLETED
    assert post.item['albumId'] == album.id

    # check our post count - should have incremented
    album.refresh_item()
    assert album.item.get('postCount', 0) == 1
    posted_by_user.refresh_item()
    assert posted_by_user.item.get('postCount', 0) == 1

    # check calls to mocked out managers
    media.refresh_item()
    post.item['mediaObjects'] = [media.item]
    assert post.followed_first_story_manager.mock_calls == []
    assert post.feed_manager.mock_calls == [
        call.add_post_to_followers_feeds(posted_by_user.id, post.item),
    ]
    assert post.album_manager.update_album_art_if_needed.mock_calls == [
        call(album.id),
    ]