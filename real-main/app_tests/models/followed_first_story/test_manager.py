from uuid import uuid4

import pendulum
import pytest

from app.models.follow.enums import FollowStatus
from app.models.post.enums import PostType


@pytest.fixture
def following_user_ids(user_manager, follow_manager, cognito_client):
    "A pair of user ids for which one follows the other"
    cognito_client.boto_client.admin_create_user(UserPoolId=cognito_client.user_pool_id, Username='fruid')
    cognito_client.boto_client.admin_create_user(UserPoolId=cognito_client.user_pool_id, Username='fduid')
    follower_user = user_manager.create_cognito_only_user('fruid', 'frUname')
    followed_user = user_manager.create_cognito_only_user('fduid', 'fdUname')
    follow_manager.dynamo.client.transact_write_items([
        follow_manager.dynamo.transact_add_following(follower_user.id, followed_user.id, FollowStatus.FOLLOWING),
    ])
    return (follower_user.id, followed_user.id)


@pytest.fixture
def followed_posts(post_manager, dynamo_client, following_user_ids):
    "A quintet of completed posts by the followed user in the DB, none of them with expiresAt"
    user_id = following_user_ids[1]
    posts = [
        post_manager.add_post(user_id, str(uuid4()), PostType.TEXT_ONLY, text='lore ipsum'),
        post_manager.add_post(user_id, str(uuid4()), PostType.TEXT_ONLY, text='lore ipsum'),
        post_manager.add_post(user_id, str(uuid4()), PostType.TEXT_ONLY, text='lore ipsum'),
        post_manager.add_post(user_id, str(uuid4()), PostType.TEXT_ONLY, text='lore ipsum'),
        post_manager.add_post(user_id, str(uuid4()), PostType.TEXT_ONLY, text='lore ipsum'),
    ]
    yield [post.item for post in posts]


def test_generate_batched_follower_user_ids_none(ffs_manager):
    followed_user_id = 'fid'
    resp = list(ffs_manager.generate_batched_follower_user_ids(followed_user_id))
    assert resp == []


def test_generate_batched_follower_user_ids_filters_out_wrong_status(ffs_manager, follow_manager):
    followed_user_id = 'fid'
    follow_manager.dynamo.client.transact_write_items([
        follow_manager.dynamo.transact_add_following(str(uuid4()), followed_user_id, FollowStatus.REQUESTED),
    ])
    resp = list(ffs_manager.generate_batched_follower_user_ids(followed_user_id))
    assert resp == []


def test_generate_batched_follower_user_one(ffs_manager, follow_manager):
    followed_user_id = 'fid'
    follower_user_id = 'followeruid'
    follow_manager.dynamo.client.transact_write_items([
        follow_manager.dynamo.transact_add_following(follower_user_id, followed_user_id, FollowStatus.FOLLOWING),
    ])
    resp = list(ffs_manager.generate_batched_follower_user_ids(followed_user_id))
    assert len(resp) == 1
    assert len(resp[0]) == 1
    assert resp[0][0] == follower_user_id


def test_generate_batched_follower_user_many(ffs_manager, follow_manager):
    followed_user_id = 'fid'

    transact_items_5 = [
        follow_manager.dynamo.transact_add_following(str(uuid4()), followed_user_id, FollowStatus.FOLLOWING)
        for i in range(0, 5)
    ]
    follow_manager.dynamo.client.transact_write_items(transact_items_5)
    resp = list(ffs_manager.generate_batched_follower_user_ids(followed_user_id))
    assert len(resp) == 1
    assert len(resp[0]) == 5

    transact_items_20 = [
        follow_manager.dynamo.transact_add_following(str(uuid4()), followed_user_id, FollowStatus.FOLLOWING)
        for i in range(0, 20)
    ]
    follow_manager.dynamo.client.transact_write_items(transact_items_20)
    resp = list(ffs_manager.generate_batched_follower_user_ids(followed_user_id))
    assert len(resp) == 1
    assert len(resp[0]) == 25

    transact_items_1 = [
        follow_manager.dynamo.transact_add_following(str(uuid4()), followed_user_id, FollowStatus.FOLLOWING)
        for i in range(0, 1)
    ]
    follow_manager.dynamo.client.transact_write_items(transact_items_1)
    resp = list(ffs_manager.generate_batched_follower_user_ids(followed_user_id))
    assert len(resp) == 2
    assert len(resp[0]) == 25
    assert len(resp[1]) == 1

    transact_items_25 = [
        follow_manager.dynamo.transact_add_following(str(uuid4()), followed_user_id, FollowStatus.FOLLOWING)
        for i in range(0, 25)
    ]
    follow_manager.dynamo.client.transact_write_items(transact_items_25)
    resp = list(ffs_manager.generate_batched_follower_user_ids(followed_user_id))
    assert len(resp) == 3
    assert len(resp[0]) == 25
    assert len(resp[1]) == 25
    assert len(resp[2]) == 1


def test_refresh_after_remove_story_not_yet_in_db(ffs_manager, following_user_ids, followed_posts, dynamo_client):
    follower_user_id, followed_user_id = following_user_ids
    post = followed_posts[0]

    # check no ffs in the DB
    followed_first_story_pk = {
        'partitionKey': f'followedFirstStory/{follower_user_id}/{followed_user_id}',
        'sortKey': '-',
    }
    assert dynamo_client.get_item(followed_first_story_pk) is None

    # make that post into a story, but don't write that to the DB
    post['expiresAt'] = pendulum.now('utc').to_iso8601_string()

    # refresh as if after remove, story isn't in the DB
    ffs_manager.refresh_after_story_change(story_prev=post)

    # check still no ffs in the DB
    followed_first_story_pk = {
        'partitionKey': f'followedFirstStory/{follower_user_id}/{followed_user_id}',
        'sortKey': '-',
    }
    assert dynamo_client.get_item(followed_first_story_pk) is None


def test_refresh_after_add_story_not_yet_in_db(ffs_manager, following_user_ids, followed_posts, dynamo_client):
    follower_user_id, followed_user_id = following_user_ids
    post = followed_posts[0]

    # make that post into a story, but don't write that to the DB
    post['expiresAt'] = pendulum.now('utc').to_iso8601_string()

    # check no ffs in the DB
    followed_first_story_pk = {
        'partitionKey': f'followedFirstStory/{follower_user_id}/{followed_user_id}',
        'sortKey': '-',
    }
    assert dynamo_client.get_item(followed_first_story_pk) is None

    # refresh as if after add, story isn't yet in the DB, check ffs now in db
    ffs_manager.refresh_after_story_change(story_now=post)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post['postId']


def test_refresh_after_add_story_in_db(ffs_manager, following_user_ids, followed_posts, dynamo_client, post_manager):
    follower_user_id, followed_user_id = following_user_ids
    post = followed_posts[0]

    # check no ffs in the DB
    followed_first_story_pk = {
        'partitionKey': f'followedFirstStory/{follower_user_id}/{followed_user_id}',
        'sortKey': '-',
    }
    assert dynamo_client.get_item(followed_first_story_pk) is None

    # add story to DB, refresh, check ffs now in db
    post = post_manager.dynamo.set_expires_at(post, pendulum.now('utc'))
    ffs_manager.refresh_after_story_change(story_now=post)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post['postId']


def test_refresh_after_add_story_order(ffs_manager, following_user_ids, followed_posts, dynamo_client, post_manager):
    follower_user_id, followed_user_id = following_user_ids
    post1, post2, post3 = followed_posts[:3]

    now = pendulum.now('utc')
    in_one_hour = now + pendulum.duration(hours=1)
    in_two_hours = now + pendulum.duration(hours=2)
    in_three_hours = now + pendulum.duration(hours=3)

    # change the middle post to a story, save to db
    post2 = post_manager.dynamo.set_expires_at(post2, in_two_hours)
    ffs_manager.refresh_after_story_change(story_now=post2)

    # check ffs exists in the DB
    followed_first_story_pk = {
        'partitionKey': f'followedFirstStory/{follower_user_id}/{followed_user_id}',
        'sortKey': '-',
    }
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post2['postId']

    # change the last post to a story, save to db, ffs should not have chagned
    post3 = post_manager.dynamo.set_expires_at(post3, in_three_hours)
    ffs_manager.refresh_after_story_change(story_now=post3)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post2['postId']

    # change the first post to a story, save to db, ffs should now be the new one
    post1 = post_manager.dynamo.set_expires_at(post1, in_one_hour)
    ffs_manager.refresh_after_story_change(story_now=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post1['postId']


def test_refresh_remove_story_order(ffs_manager, following_user_ids, followed_posts, dynamo_client, post_manager):
    follower_user_id, followed_user_id = following_user_ids
    post1, post2, post3, post4, post5 = followed_posts

    now = pendulum.now('utc')
    in_one_hour = now + pendulum.duration(hours=1)
    in_two_hours = now + pendulum.duration(hours=2)
    in_three_hours = now + pendulum.duration(hours=3)
    in_four_hours = now + pendulum.duration(hours=4)
    in_five_hours = now + pendulum.duration(hours=5)

    # make all of those stories
    post1 = post_manager.dynamo.set_expires_at(post1, in_one_hour)
    ffs_manager.refresh_after_story_change(story_now=post1)
    post2 = post_manager.dynamo.set_expires_at(post2, in_two_hours)
    ffs_manager.refresh_after_story_change(story_now=post2)
    post3 = post_manager.dynamo.set_expires_at(post3, in_three_hours)
    ffs_manager.refresh_after_story_change(story_now=post3)
    post4 = post_manager.dynamo.set_expires_at(post4, in_four_hours)
    ffs_manager.refresh_after_story_change(story_now=post4)
    post5 = post_manager.dynamo.set_expires_at(post5, in_five_hours)
    ffs_manager.refresh_after_story_change(story_now=post5)

    # refresh the ffs, make sure it's what we expect
    followed_first_story_pk = {
        'partitionKey': f'followedFirstStory/{follower_user_id}/{followed_user_id}',
        'sortKey': '-',
    }
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post1['postId']

    # remove one from DB that doesn't change order, check ffs should not have changed
    post_manager.dynamo.remove_expires_at(post3['postId'])
    ffs_manager.refresh_after_story_change(story_prev=post3)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post1['postId']

    # remove one from DB that does change order, check ffs should have changed
    post_manager.dynamo.remove_expires_at(post1['postId'])
    ffs_manager.refresh_after_story_change(story_prev=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post2['postId']

    # do the refresh first and removal second (dynamo order of operations not guaranteed), should not change order
    ffs_manager.refresh_after_story_change(story_prev=post4)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post2['postId']
    post_manager.dynamo.remove_expires_at(post4['postId'])

    # do the refresh first and removal second (dynamo order of operations not guaranteed), should change order
    ffs_manager.refresh_after_story_change(story_prev=post2)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post5['postId']
    post_manager.dynamo.remove_expires_at(post2['postId'])


def test_refresh_change_story_order(ffs_manager, following_user_ids, followed_posts, dynamo_client, post_manager):
    follower_user_id, followed_user_id = following_user_ids
    post1, post2 = followed_posts[:2]

    now = pendulum.now('utc')
    in_one_hour = now + pendulum.duration(hours=1)
    in_two_hours = now + pendulum.duration(hours=2)
    in_three_hours = now + pendulum.duration(hours=3)
    in_four_hours = now + pendulum.duration(hours=4)
    in_five_hours = now + pendulum.duration(hours=5)

    # make all of those stories
    post1 = post_manager.dynamo.set_expires_at(post1, in_two_hours)
    ffs_manager.refresh_after_story_change(story_now=post1)
    post2 = post_manager.dynamo.set_expires_at(post2, in_three_hours)
    ffs_manager.refresh_after_story_change(story_now=post2)

    # refresh the ffs, make sure it's what we expect
    followed_first_story_pk = {
        'partitionKey': f'followedFirstStory/{follower_user_id}/{followed_user_id}',
        'sortKey': '-',
    }
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post1['postId']

    # move post1 expiresAt up, does not change ordering
    story_prev = post1.copy()
    post1 = post_manager.dynamo.set_expires_at(post1, in_one_hour)
    ffs_manager.refresh_after_story_change(story_prev=story_prev, story_now=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post1['postId']

    # move post1 expiresAt back, does not change ordering
    story_prev = post1.copy()
    post1 = post_manager.dynamo.set_expires_at(post1, in_two_hours)
    ffs_manager.refresh_after_story_change(story_prev=story_prev, story_now=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post1['postId']

    # move post1 expiresAt back, does change ordering
    story_prev = post1.copy()
    post1 = post_manager.dynamo.set_expires_at(post1, in_four_hours)
    ffs_manager.refresh_after_story_change(story_prev=story_prev, story_now=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post2['postId']

    # move post1 expiresAt back, does not change ordering
    story_prev = post1.copy()
    post1 = post_manager.dynamo.set_expires_at(post1, in_five_hours)
    ffs_manager.refresh_after_story_change(story_prev=story_prev, story_now=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post2['postId']

    # move post1 expiresAt up, does not change ordering
    story_prev = post1.copy()
    post1 = post_manager.dynamo.set_expires_at(post1, in_four_hours)
    ffs_manager.refresh_after_story_change(story_prev=story_prev, story_now=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post2['postId']

    # move post1 expiresAt up, does change ordering
    story_prev = post1.copy()
    post1 = post_manager.dynamo.set_expires_at(post1, in_two_hours)
    ffs_manager.refresh_after_story_change(story_prev=story_prev, story_now=post1)
    resp = dynamo_client.get_item(followed_first_story_pk)
    assert resp['postId'] == post1['postId']
